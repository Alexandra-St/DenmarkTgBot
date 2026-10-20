"""Snapshot-native Folketinget ingestion, separate from OData checkpoints.

This client accepts extracted snapshot candidates only. Historical live
submission is intentionally disabled because candidate extraction is a diff,
not a complete initial Folketinget dataset.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from collectors import ingestion_client
from collectors.relevance import DIRECT, POTENTIAL, RULESET_VERSION

USER_AGENT = "DenmarkTgBot-SnapshotIngestionClient/1.0"
MAX_COMMIT_ATTEMPTS = 10
REQUEST_TIMEOUT_SECONDS = 15
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BACKUP_TIMESTAMP = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\.(\d{3})$")
MAX_HISTORICAL_SELECTED_RECORDS = 10_000
MAX_HISTORICAL_SELECTED_BYTES = 200 * ingestion_client.MAX_BATCH_BYTES
MAX_HISTORICAL_BATCHES = 200
Opener = Callable[..., Any]

WORKER_ERRORS = frozenset({
    "snapshot_base_stale", "snapshot_already_current", "snapshot_regression",
    "snapshot_identity_conflict", "snapshot_schema_changed", "historical_baseline_required",
    "source_run_active_or_run_conflict", "run_id_conflict", "incomplete_publication",
    "snapshot_commit_conflict", "snapshot_state_not_initialized", "snapshot_base_required",
    "snapshot_identity_invalid", "snapshot_base_must_be_null", "snapshot_checkpoints_not_allowed",
    "snapshot_state_already_initialized",
    "snapshot_source_unsupported", "incomplete_collection", "invalid_run_id", "invalid_mode",
    "invalid_batch_count", "invalid_batch", "invalid_batch_index", "batch_idempotency_conflict",
    "batch_conflict", "publication_batch_conflict", "invalid_publication", "invalid_publication_id",
    "invalid_publication_fields", "invalid_historical_flag", "invalid_completeness",
    "invalid_classification", "invalid_snapshot", "publication_too_large", "batches_incomplete",
})


@dataclass(frozen=True)
class SnapshotIdentity:
    backup_sha256: str
    backup_finish_date: str
    schema_fingerprint: str

    def to_dict(self) -> dict[str, str]:
        return {
            "backup_sha256": self.backup_sha256,
            "backup_finish_date": self.backup_finish_date,
            "schema_fingerprint": self.schema_fingerprint,
        }


class SnapshotIngestionError(ValueError):
    """Safe failure carrying only a fixed code, stage and optional run UUID."""

    def __init__(self, code: str, run_id: str | None = None, stage: str | None = None) -> None:
        self.code = code
        self.run_id = run_id
        self.stage = stage
        suffix = f" run_id={run_id}" if run_id else ""
        super().__init__(code + suffix)


def validate_snapshot_identity(value: object) -> SnapshotIdentity:
    if not isinstance(value, Mapping) or set(value) != {"backup_sha256", "backup_finish_date", "schema_fingerprint"}:
        raise SnapshotIngestionError("snapshot_identity_invalid")
    backup_sha = value.get("backup_sha256")
    finish = value.get("backup_finish_date")
    fingerprint = value.get("schema_fingerprint")
    if not isinstance(backup_sha, str) or not _SHA256.fullmatch(backup_sha):
        raise SnapshotIngestionError("snapshot_identity_invalid")
    if not isinstance(fingerprint, str) or not _SHA256.fullmatch(fingerprint):
        raise SnapshotIngestionError("snapshot_identity_invalid")
    if not isinstance(finish, str):
        raise SnapshotIngestionError("snapshot_identity_invalid")
    match = _BACKUP_TIMESTAMP.fullmatch(finish)
    if not match:
        raise SnapshotIngestionError("snapshot_identity_invalid")
    try:
        year, month, day, hour, minute, second, millisecond = map(int, match.groups())
        datetime(year, month, day, hour, minute, second, millisecond * 1000)
    except (ValueError, OverflowError):
        raise SnapshotIngestionError("snapshot_identity_invalid") from None
    return SnapshotIdentity(backup_sha, finish, fingerprint)


def _validate_pair(mode: str, base_value: object, target_value: object) -> tuple[SnapshotIdentity | None, SnapshotIdentity]:
    if mode not in {"historical", "incremental"}:
        raise SnapshotIngestionError("invalid_mode")
    target = validate_snapshot_identity(target_value)
    if mode == "historical":
        if base_value is not None:
            raise SnapshotIngestionError("snapshot_base_must_be_null")
        return None, target
    if base_value is None:
        raise SnapshotIngestionError("snapshot_base_required")
    base = validate_snapshot_identity(base_value)
    if target.backup_finish_date < base.backup_finish_date:
        raise SnapshotIngestionError("snapshot_regression")
    if target.backup_finish_date == base.backup_finish_date:
        if target == base:
            raise SnapshotIngestionError("snapshot_already_current")
        raise SnapshotIngestionError("snapshot_identity_conflict")
    if target.schema_fingerprint != base.schema_fingerprint:
        raise SnapshotIngestionError("snapshot_schema_changed")
    return base, target


def _candidate_cases(bundle: object) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(bundle, Mapping) or bundle.get("source") != "folketinget" or not isinstance(bundle.get("complete"), bool):
        raise SnapshotIngestionError("candidate_bundle_invalid")
    cases = bundle.get("cases")
    if not isinstance(cases, list) or any(not isinstance(case, Mapping) for case in cases):
        raise SnapshotIngestionError("candidate_bundle_invalid")
    return [dict(case) for case in cases], bundle["complete"]


def _relevance_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"DIRECT": 0, "POTENTIAL": 0, "IRRELEVANT": 0}
    for record in records:
        relevance = record.get("relevance")
        if relevance not in counts:
            raise SnapshotIngestionError("prepared_record_invalid")
        counts[relevance] += 1
    return counts


def _validate_prepared_historical_record(value: object) -> dict[str, Any]:
    """Validate the already-prepared publication contract without reclassifying it."""
    if not isinstance(value, Mapping):
        raise SnapshotIngestionError("prepared_record_invalid")
    record = dict(value)
    if (record.get("source") != "folketinget" or record.get("historical") is not True
            or record.get("complete") is not True or record.get("relevance") not in {DIRECT, POTENTIAL}
            or record.get("ruleset_version") != RULESET_VERSION):
        raise SnapshotIngestionError("prepared_record_invalid")
    external_id = record.get("external_id")
    title = record.get("title")
    if (not isinstance(external_id, str) or not re.fullmatch(r"\d{1,20}", external_id)
            or not isinstance(title, str) or not title.strip()):
        raise SnapshotIngestionError("prepared_record_invalid")
    url = record.get("url")
    try:
        parsed_url = urlsplit(url) if isinstance(url, str) else None
        hostname = parsed_url.hostname if parsed_url else None
    except ValueError:
        parsed_url = None
        hostname = None
    if (parsed_url is None or parsed_url.scheme != "https" or hostname not in {"oda.ft.dk", "www.ft.dk"}
            or parsed_url.username is not None or parsed_url.password is not None
            or parsed_url.query or parsed_url.fragment):
        raise SnapshotIngestionError("prepared_record_invalid")
    if (not isinstance(record.get("notification_blocked"), bool)
            or not isinstance(record.get("diagnostics"), list)
            or not isinstance(record.get("source_metadata"), Mapping)
            or not isinstance(record.get("relevance_reason"), str)):
        raise SnapshotIngestionError("prepared_record_invalid")
    for field in ("status", "publication_type", "published_at", "source_updated_at", "source_updated_at_raw"):
        if record.get(field) is not None and not isinstance(record.get(field), str):
            raise SnapshotIngestionError("prepared_record_invalid")
    snapshot = record.get("normalized_snapshot")
    if (not isinstance(snapshot, Mapping) or snapshot.get("title") != title
            or snapshot.get("content_text") is not None and not isinstance(snapshot.get("content_text"), str)
            or snapshot.get("status") is not None and not isinstance(snapshot.get("status"), str)
            or snapshot.get("publication_type") is not None and not isinstance(snapshot.get("publication_type"), str)
            or not all(isinstance(snapshot.get(name), list) for name in ("documents", "stages", "votes"))):
        raise SnapshotIngestionError("prepared_record_invalid")
    for name in ("documents", "stages", "votes"):
        if any(not isinstance(item, Mapping) for item in snapshot[name]):
            raise SnapshotIngestionError("prepared_record_invalid")
    return record


def dry_run_prepared_historical_records(
    records: object,
    *,
    mode: str = "historical",
    state_kind: str = "snapshot",
    dry_run: bool = True,
    base_snapshot: object,
    target_snapshot: object,
) -> dict[str, Any]:
    """Validate already-prepared historical records and batch them, with no network path."""
    if mode != "historical":
        raise SnapshotIngestionError("invalid_mode")
    if state_kind != "snapshot":
        raise SnapshotIngestionError("snapshot_state_kind_invalid")
    if dry_run is not True:
        raise SnapshotIngestionError("historical_snapshot_submission_not_enabled")
    if base_snapshot is not None:
        raise SnapshotIngestionError("snapshot_base_must_be_null")
    _, target = _validate_pair("historical", base_snapshot, target_snapshot)
    if not isinstance(records, list) or len(records) > MAX_HISTORICAL_SELECTED_RECORDS:
        raise SnapshotIngestionError("prepared_record_limit_exceeded")
    try:
        prepared = [_validate_prepared_historical_record(item) for item in records]
        encoded_size = len(json.dumps(prepared, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if encoded_size > MAX_HISTORICAL_SELECTED_BYTES:
            raise SnapshotIngestionError("prepared_record_limit_exceeded")
        batches = ingestion_client.chunks(prepared)
        counts = _relevance_counts(prepared)
    except SnapshotIngestionError:
        raise
    except Exception:
        raise SnapshotIngestionError("prepared_record_invalid") from None
    if len(batches) > MAX_HISTORICAL_BATCHES:
        raise SnapshotIngestionError("prepared_batch_limit_exceeded")
    return {
        "dry_run": True,
        "mode": "historical",
        "state_kind": "snapshot",
        "records": len(prepared),
        "batches": len(batches),
        "relevance_counts": counts,
        "collection_complete": True,
        "submission_eligible": True,
        "base_snapshot": None,
        "target_snapshot": target.to_dict(),
    }


def _ingest_url(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint:
        raise SnapshotIngestionError("ingestion_configuration_missing")
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
    except ValueError:
        raise SnapshotIngestionError("ingestion_endpoint_invalid") from None
    if parsed.scheme != "https" or not hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise SnapshotIngestionError("ingestion_endpoint_invalid")
    return endpoint.rstrip("/") + "/internal/ingest"


def _safe_worker_error(value: object) -> str:
    if isinstance(value, Mapping):
        code = value.get("error")
        if isinstance(code, str) and code in WORKER_ERRORS:
            return code
    return "worker_request_rejected"


def _post(url: str, token: str, payload: Mapping[str, Any], run_id: str, stage: str, opener: Opener) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        try:
            parsed_error = json.loads(error.read().decode("utf-8"))
        except Exception:
            parsed_error = None
        raise SnapshotIngestionError(_safe_worker_error(parsed_error), run_id, stage) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        # Never retry a write after an ambiguous transport failure. Resume with this same run_id.
        raise SnapshotIngestionError("ingestion_transport_failed", run_id, stage) from None
    except Exception:
        # Keep unexpected opener/client failures inside the safe error contract.
        raise SnapshotIngestionError("ingestion_transport_failed", run_id, stage) from None
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SnapshotIngestionError("worker_response_invalid", run_id, stage) from None
    if not isinstance(result, dict):
        raise SnapshotIngestionError("worker_response_invalid", run_id, stage)
    if "error" in result:
        raise SnapshotIngestionError(_safe_worker_error(result), run_id, stage)
    return result


def _require_run_response(value: Mapping[str, Any], run_id: str, stage: str) -> None:
    if value.get("run_id") != run_id:
        raise SnapshotIngestionError("worker_response_invalid", run_id, stage)


def ingest_snapshot_candidates(
    candidate_bundle: object,
    *,
    mode: str,
    base_snapshot: object,
    target_snapshot: object,
    endpoint: str = "",
    token: str = "",
    run_id: str | None = None,
    dry_run: bool = True,
    opener: Opener = urllib.request.urlopen,
    max_commit_attempts: int = MAX_COMMIT_ATTEMPTS,
) -> dict[str, Any]:
    """Prepare or submit an incremental snapshot run; never uses OData state."""
    if not isinstance(dry_run, bool):
        raise SnapshotIngestionError("invalid_dry_run")
    if mode == "historical" and not dry_run:
        raise SnapshotIngestionError("historical_snapshot_submission_not_enabled", run_id)
    base, target = _validate_pair(mode, base_snapshot, target_snapshot)
    cases, complete = _candidate_cases(candidate_bundle)
    if mode == "incremental" and not dry_run and not complete:
        raise SnapshotIngestionError("incomplete_candidates", run_id)
    try:
        records = ingestion_client.prepare_records({"cases": cases}, source="folketinget", mode=mode)
        batches = ingestion_client.chunks(records)
        counts = _relevance_counts(records)
    except SnapshotIngestionError:
        raise
    except Exception:
        raise SnapshotIngestionError("candidate_records_invalid", run_id) from None

    if run_id is not None and not isinstance(run_id, str):
        raise SnapshotIngestionError("invalid_run_id")
    run_id = run_id or str(uuid.uuid4())
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", run_id, re.IGNORECASE):
        raise SnapshotIngestionError("invalid_run_id", run_id)
    eligible = mode == "incremental" and complete
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "mode": mode,
        "state_kind": "snapshot",
        "run_id": run_id,
        "records": len(records),
        "batches": len(batches),
        "relevance_counts": counts,
        "collection_complete": complete,
        "submission_eligible": eligible,
        "base_snapshot": base.to_dict() if base else None,
        "target_snapshot": target.to_dict(),
    }
    if dry_run:
        return summary

    if not isinstance(token, str) or not token:
        raise SnapshotIngestionError("ingestion_configuration_missing", run_id, "start")
    ingest_url = _ingest_url(endpoint)
    common = {"action": "start", "run_id": run_id, "source": "folketinget", "mode": mode, "state_kind": "snapshot"}
    started = _post(ingest_url, token, {
        **common,
        "expected_batches": len(batches),
        "collection_complete": True,
        "base_snapshot": base.to_dict() if base else None,
        "target_snapshot": target.to_dict(),
    }, run_id, "start", opener)
    _require_run_response(started, run_id, "start")
    state = started.get("status")
    expected_batches = started.get("expected_batches")
    if (started.get("accepted") is not True or started.get("state_kind") != "snapshot"
            or state not in {"OPEN", "COMMITTING", "COMMITTED", "BLOCKED"}
            or isinstance(expected_batches, bool) or not isinstance(expected_batches, int)
            or expected_batches != len(batches)):
        raise SnapshotIngestionError("worker_response_invalid", run_id, "start")
    if state != "OPEN" and started.get("idempotent") is not True:
        raise SnapshotIngestionError("worker_response_invalid", run_id, "start")

    accepted = 0
    if state == "OPEN":
        for index, batch in enumerate(batches):
            result = _post(ingest_url, token, {"action": "batch", "run_id": run_id, "batch_index": index, "records": batch}, run_id, "batch", opener)
            _require_run_response(result, run_id, "batch")
            batch_count = result.get("accepted_records")
            if (result.get("accepted") is not True or result.get("batch_index") != index
                    or isinstance(batch_count, bool) or not isinstance(batch_count, int)
                    or batch_count != len(batch)):
                raise SnapshotIngestionError("worker_response_invalid", run_id, "batch")
            accepted += batch_count

    attempts = max_commit_attempts
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1 or attempts > MAX_COMMIT_ATTEMPTS:
        raise SnapshotIngestionError("commit_retry_limit_invalid", run_id, "commit")
    result: dict[str, Any] | None = None
    for _ in range(attempts):
        result = _post(ingest_url, token, {"action": "commit", "run_id": run_id}, run_id, "commit", opener)
        _require_run_response(result, run_id, "commit")
        status = result.get("status")
        if status == "COMMITTING":
            processed = result.get("processed_records")
            remaining = result.get("remaining_records")
            if (result.get("checkpoints_advanced") is not False or result.get("snapshot_advanced") is not False
                    or isinstance(processed, bool) or not isinstance(processed, int) or processed < 0
                    or isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0):
                raise SnapshotIngestionError("worker_response_invalid", run_id, "commit")
            continue
        if status == "COMMITTED":
            generation = result.get("snapshot_generation")
            processed = result.get("processed_records")
            if (result.get("checkpoints_advanced") is not False or result.get("snapshot_advanced") is not True
                    or isinstance(generation, bool) or not isinstance(generation, int) or generation < 1
                    or isinstance(processed, bool) or not isinstance(processed, int) or processed < 0):
                raise SnapshotIngestionError("worker_response_invalid", run_id, "commit")
            summary.update({"status": status, "accepted_records": accepted, "snapshot_advanced": True, "snapshot_generation": generation})
            return summary
        if status == "BLOCKED":
            if (result.get("checkpoints_advanced") is not False or result.get("snapshot_advanced") is not False
                    or ("error" in result and result.get("error") != "incomplete_publication")):
                raise SnapshotIngestionError("worker_response_invalid", run_id, "commit")
            summary.update({"status": status, "accepted_records": accepted, "snapshot_advanced": False})
            return summary
        raise SnapshotIngestionError("worker_response_invalid", run_id, "commit")
    if result and result.get("status") == "COMMITTING":
        raise SnapshotIngestionError("commit_retry_limit", run_id, "commit")
    raise SnapshotIngestionError("worker_response_invalid", run_id, "commit")


def _read_json_file(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SnapshotIngestionError("input_file_invalid") from None


def main(argv: list[str] | None = None, opener: Opener = urllib.request.urlopen) -> int:
    parser = argparse.ArgumentParser(description="Dry-run or submit Folketinget snapshot candidates.")
    parser.add_argument("candidate_bundle", type=Path)
    parser.add_argument("--mode", choices=("historical", "incremental"), required=True)
    parser.add_argument("--base-snapshot", type=Path)
    parser.add_argument("--target-snapshot", type=Path, required=True)
    parser.add_argument("--run-id")
    live_mode = parser.add_mutually_exclusive_group()
    live_mode.add_argument("--dry-run", action="store_true", default=True)
    live_mode.add_argument("--live", action="store_false", dest="dry_run", help="Explicitly allow an incremental live submission")
    args = parser.parse_args(argv)
    try:
        bundle = _read_json_file(args.candidate_bundle)
        base = _read_json_file(args.base_snapshot) if args.base_snapshot else None
        target = _read_json_file(args.target_snapshot)
        result = ingest_snapshot_candidates(
            bundle,
            mode=args.mode,
            base_snapshot=base,
            target_snapshot=target,
            endpoint=os.environ.get("INGEST_API_URL", ""),
            token=os.environ.get("INGEST_API_TOKEN", ""),
            run_id=args.run_id,
            dry_run=args.dry_run,
            opener=opener,
        )
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        if args.dry_run:
            # Historical dry-runs are intentionally never submit-eligible, but
            # remain successful validation runs. Incomplete incremental dry-runs
            # stay visible as a nonzero result.
            return 0 if args.mode == "historical" or result.get("collection_complete") is True else 2
        return 0 if result.get("status") == "COMMITTED" else 2
    except SnapshotIngestionError as error:
        print(json.dumps({"error": error.code, **({"run_id": error.run_id} if error.run_id else {}),
                          **({"stage": error.stage} if error.stage else {})}, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
