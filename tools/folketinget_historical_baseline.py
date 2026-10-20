#!/usr/bin/env python3
"""Streaming, offline preparation of a complete historical Folketinget baseline."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Callable

REPOSITORY_ROOT = str(Path(__file__).resolve().parents[1])
if REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, REPOSITORY_ROOT)
TOOLS_DIRECTORY = str(Path(__file__).resolve().parent)
if TOOLS_DIRECTORY not in sys.path:
    sys.path.insert(0, TOOLS_DIRECTORY)

from collectors import ingestion_client
from collectors.relevance import DIRECT, IRRELEVANT, POTENTIAL, RULESET_VERSION
import folketinget_candidate_extraction as candidate_extraction
import folketinget_snapshot_diff as snapshot_diff
import folketinget_snapshot_manifest as snapshot_manifest
from folketinget_sql_transport import CHUNK_LINE_LENGTH_BUCKETS, CHUNK_ROW_SHAPES


MAX_SAGS_PER_HYDRATION = 250
RELEVANCE_VALUES = frozenset({DIRECT, POTENTIAL, IRRELEVANT})


class HistoricalBaselineError(Exception):
    """Safe fail-closed category with no source data."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _readonly_manifest(path: Path) -> sqlite3.Connection:
    try:
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        return connection
    except (OSError, sqlite3.Error, ValueError):
        raise HistoricalBaselineError("manifest_open_failed") from None


def _manifest_sag_count(path: Path) -> int:
    try:
        snapshot_diff.validate_manifest(path)
    except snapshot_diff.DiffError as error:
        raise HistoricalBaselineError(error.category) from None
    connection = _readonly_manifest(path)
    try:
        row = connection.execute("SELECT row_count FROM entity_counts WHERE entity=?", ("Sag",)).fetchone()
        if row is None or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 0:
            raise HistoricalBaselineError("manifest_sag_count_invalid")
        return row[0]
    except sqlite3.Error:
        raise HistoricalBaselineError("manifest_sag_count_invalid") from None
    finally:
        connection.close()


def _iter_sag_id_batches(path: Path, batch_size: int):
    """Yield decoded Sag keys from one ordered SQLite cursor, never materializing the universe."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_SAGS_PER_HYDRATION:
        raise HistoricalBaselineError("historical_batch_size_invalid")
    connection = _readonly_manifest(path)
    try:
        cursor = connection.execute('SELECT canonical_key FROM "rows" WHERE entity=? ORDER BY canonical_key', ("Sag",))
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            ids: list[int] = []
            for (canonical_key,) in rows:
                if not isinstance(canonical_key, str):
                    raise HistoricalBaselineError("canonical_key_invalid")
                try:
                    ids.append(candidate_extraction.decode_canonical_key("Sag", canonical_key))
                except candidate_extraction.CandidateExtractionError:
                    raise HistoricalBaselineError("canonical_key_invalid") from None
            if len(ids) != len(set(ids)):
                raise HistoricalBaselineError("duplicate_sag_key")
            yield tuple(ids)
    except HistoricalBaselineError:
        raise
    except sqlite3.Error:
        raise HistoricalBaselineError("manifest_sag_enumeration_failed") from None
    finally:
        connection.close()


def _adaptive_provider_metrics(provider: object) -> dict[str, object]:
    """Read only the SQL provider's aggregate counters, with a zero default for other providers."""
    getter = getattr(provider, "adaptive_summary", None)
    if getter is None:
        return {
            "provider_adaptive_splits": 0,
            "provider_adaptive_split_successes": 0,
            "provider_max_split_depth": 0,
            "provider_terminal_singleton_failures": 0,
            "provider_terminal_transport_row_shapes": {},
            "provider_terminal_transport_line_length_buckets": {},
        }
    if not callable(getter):
        raise HistoricalBaselineError("provider_metrics_invalid")
    try:
        result = getter()
    except Exception:
        raise HistoricalBaselineError("provider_metrics_invalid") from None
    if not isinstance(result, dict):
        raise HistoricalBaselineError("provider_metrics_invalid")
    values = (
        result.get("adaptive_split_count"),
        result.get("adaptive_split_success_count"),
        result.get("adaptive_split_max_depth"),
        result.get("adaptive_terminal_failure_count", 0),
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise HistoricalBaselineError("provider_metrics_invalid")
    row_shapes = result.get("adaptive_terminal_failures_by_row_shape", {})
    line_buckets = result.get("adaptive_terminal_failures_by_line_length_bucket", {})
    for values_map, allowed in ((row_shapes, CHUNK_ROW_SHAPES), (line_buckets, CHUNK_LINE_LENGTH_BUCKETS)):
        if not isinstance(values_map, dict) or any(
            key not in allowed or isinstance(count, bool) or not isinstance(count, int) or count < 1
            for key, count in values_map.items()
        ):
            raise HistoricalBaselineError("provider_metrics_invalid")
        if sum(values_map.values()) > values[3]:
            raise HistoricalBaselineError("provider_metrics_invalid")
    return {
        "provider_adaptive_splits": values[0],
        "provider_adaptive_split_successes": values[1],
        "provider_max_split_depth": values[2],
        "provider_terminal_singleton_failures": values[3],
        "provider_terminal_transport_row_shapes": dict(sorted(row_shapes.items())),
        "provider_terminal_transport_line_length_buckets": dict(sorted(line_buckets.items())),
    }


def run_historical_baseline(
    manifest_path: Path,
    provider: candidate_extraction.FolketingetReadOnlyProvider,
    *,
    batch_size: int = MAX_SAGS_PER_HYDRATION,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, object]:
    """Hydrate and prepare all current Sag records; performs no HTTP or submission."""
    manifest_count = _manifest_sag_count(Path(manifest_path))
    totals: Counter[str] = Counter()
    diagnostics: Counter[str] = Counter()
    processed_sags = 0
    hydrated_complete = 0
    prepared_count = 0
    incomplete_cases = 0
    ingestion_batch_count = 0
    largest_batch_records = 0
    largest_batch_bytes = 0
    # The manifest v1 primary key plus strict canonical JSON decoding makes the
    # mapping from canonical Sag key to integer ID injective. Thus no global ID
    # set is needed; only each bounded hydration group's IDs are retained.
    for sag_ids in _iter_sag_id_batches(Path(manifest_path), batch_size):
        batch_diagnostics: Counter[str] = Counter()
        try:
            cases, complete_count = candidate_extraction._hydrate_cases(set(sag_ids), provider, batch_diagnostics)
        except Exception:
            raise HistoricalBaselineError("case_hydration_failed") from None
        processed_sags += len(sag_ids)
        hydrated_complete += complete_count

        external_ids: list[int] = []
        for case in cases:
            try:
                external_id = int(case.external_id)
            except (TypeError, ValueError):
                raise HistoricalBaselineError("hydrated_case_id_invalid") from None
            external_ids.append(external_id)
            if not case.complete:
                incomplete_cases += 1
        if len(external_ids) != len(set(external_ids)):
            raise HistoricalBaselineError("duplicate_hydrated_case")
        if set(external_ids) != set(sag_ids):
            batch_diagnostics["hydrated_sag_mismatch"] += 1
            incomplete_cases += max(0, len(sag_ids) - len(external_ids))

        for code, count in batch_diagnostics.items():
            diagnostics[code if candidate_extraction.is_safe_diagnostic_code(code) else "diagnostic_invalid"] += count

        # Prepare this bounded unit independently so case payloads do not
        # accumulate across the full historical universe.
        try:
            records = ingestion_client.prepare_records(
                {"cases": [case.to_dict() for case in cases]},
                source="folketinget",
                mode="historical",
            )
            batches = ingestion_client.chunks(records)
        except Exception:
            diagnostics["ingestion_preparation_failed"] += 1
            continue

        if len(records) != complete_count:
            diagnostics["prepared_complete_count_mismatch"] += abs(len(records) - complete_count) or 1
        if len(records) != len(cases):
            diagnostics["prepared_record_count_mismatch"] += 1
        for record in records:
            if (record.get("source") != "folketinget" or record.get("historical") is not True
                    or not isinstance(record.get("complete"), bool)
                    or not isinstance(record.get("notification_blocked"), bool)
                    or record.get("relevance") not in RELEVANCE_VALUES
                    or record.get("ruleset_version") != RULESET_VERSION):
                diagnostics["prepared_record_contract_invalid"] += 1
                continue
            prepared_count += 1
            totals[str(record["relevance"])] += 1
            totals["notification_blocked"] += int(record.get("notification_blocked") is True)

        ingestion_batch_count += len(batches)
        for batch in batches:
            encoded_bytes = len(json.dumps({"records": batch}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            largest_batch_records = max(largest_batch_records, len(batch))
            largest_batch_bytes = max(largest_batch_bytes, encoded_bytes)
        if progress_callback is not None:
            try:
                progress_callback(processed_sags, manifest_count)
            except Exception:
                raise HistoricalBaselineError("progress_callback_failed") from None

    if processed_sags != manifest_count:
        raise HistoricalBaselineError("manifest_sag_count_mismatch")

    complete = (
        not diagnostics
        and incomplete_cases == 0
        and hydrated_complete == processed_sags
        and prepared_count == hydrated_complete
    )
    try:
        metadata = snapshot_diff.validate_manifest(Path(manifest_path))
    except snapshot_diff.DiffError as error:
        raise HistoricalBaselineError(error.category) from None
    return {
        "status": "PASS" if complete else "INCOMPLETE",
        "manifest_sag_count": manifest_count,
        "hydrated_case_count": hydrated_complete,
        "prepared_record_count": prepared_count,
        "DIRECT": totals[DIRECT],
        "POTENTIAL": totals[POTENTIAL],
        "IRRELEVANT": totals[IRRELEVANT],
        "notification_blocked_count": totals["notification_blocked"],
        "ingestion_batch_count": ingestion_batch_count,
        "largest_batch_records": largest_batch_records,
        "largest_batch_bytes": largest_batch_bytes,
        "incomplete_case_count": incomplete_cases,
        "diagnostics": dict(sorted(diagnostics.items())),
        "target_backup_finish": metadata["backup_finish_date"],
        "ruleset_version": RULESET_VERSION,
        **_adaptive_provider_metrics(provider),
    }


__all__ = ["HistoricalBaselineError", "MAX_SAGS_PER_HYDRATION", "run_historical_baseline"]
