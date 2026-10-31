#!/usr/bin/env python3
"""Offline core for turning Folketinget manifest changes into case candidates."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Mapping, Protocol, Sequence

REPOSITORY_ROOT = str(Path(__file__).resolve().parents[1])
if REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, REPOSITORY_ROOT)
TOOLS_DIRECTORY = str(Path(__file__).resolve().parent)
if TOOLS_DIRECTORY not in sys.path:
    sys.path.insert(0, TOOLS_DIRECTORY)

from collectors.folketinget_models import LegislativeCase, normalize_many
from collectors.folketinget_relations import assemble_cases
import folketinget_snapshot_diff as snapshot_diff
import folketinget_snapshot_manifest as snapshot_manifest
from folketinget_sql_provider import (
    RELATION_FIELDS as PROVIDER_RELATION_FIELDS,
    SAFE_PROVIDER_ERROR_CATEGORIES,
    SAFE_TRANSPORT_CATEGORIES,
    SQLProviderError,
)


SOURCE = "folketinget"
FORMAT_VERSION = 1
MAX_AFFECTED_CASES = 5000
RELATION_FIELDS = frozenset({"id", "sagid", "dokumentid", "sagstrinid", "statusid"})
INTEGER_SQL_TYPES = frozenset({"tinyint", "smallint", "int", "bigint"})
LOOKUP_METHODS = frozenset({"by_ids", "by_field"})
LOOKUP_ENTITIES = frozenset(snapshot_manifest.REQUIRED_ENTITIES)
LOOKUP_FIELDS = {entity: PROVIDER_RELATION_FIELDS[entity] for entity in LOOKUP_ENTITIES}
_SIMPLE_DIAGNOSTIC = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_SQL_MESSAGE = re.compile(r"^manifest_page_sql_msg_[0-9]{1,6}$")


class CandidateExtractionError(Exception):
    """Safe fail-closed category; never include identifiers or source values."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


class FolketingetReadOnlyProvider(Protocol):
    """Read-only row access; relation lookups must return every matching row."""

    def get_by_ids(self, entity: str, ids: Sequence[int]) -> Sequence[Mapping[str, object]]: ...

    def find_by_field(self, entity: str, field: str, values: Sequence[int]) -> Sequence[Mapping[str, object]]: ...


class InMemoryFolketingetProvider:
    """Deterministic synthetic provider for offline tests and fixtures."""

    def __init__(self, rows: Mapping[str, Sequence[Mapping[str, object]]]) -> None:
        if set(rows) - set(snapshot_manifest.REQUIRED_ENTITIES):
            raise ValueError("unsupported entity")
        self._rows = {entity: tuple(dict(row) for row in rows.get(entity, ())) for entity in snapshot_manifest.REQUIRED_ENTITIES}

    def get_by_ids(self, entity: str, ids: Sequence[int]) -> Sequence[Mapping[str, object]]:
        if entity not in self._rows:
            raise ValueError("unsupported entity")
        selected = set(ids)
        return tuple(dict(row) for row in self._rows[entity] if _is_int(row.get("id")) and row["id"] in selected)

    def find_by_field(self, entity: str, field: str, values: Sequence[int]) -> Sequence[Mapping[str, object]]:
        if entity not in self._rows or field not in RELATION_FIELDS:
            raise ValueError("unsupported lookup")
        selected = set(values)
        return tuple(dict(row) for row in self._rows[entity] if _is_int(row.get(field)) and row[field] in selected)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _safe_transport_category(value: object) -> str | None:
    if isinstance(value, str) and (
        value in SAFE_TRANSPORT_CATEGORIES or _SAFE_SQL_MESSAGE.fullmatch(value)
    ):
        return value
    return None


def _lookup_failure_code(method: str, entity: str, field: str, error: SQLProviderError) -> str | None:
    """Build an aggregate diagnostic only from provider-owned allowlists."""
    valid_field = field == "id" if method == "by_ids" else (
        method == "by_field" and entity in LOOKUP_ENTITIES and field in LOOKUP_FIELDS[entity]
    )
    if (method not in LOOKUP_METHODS or entity not in LOOKUP_ENTITIES or not valid_field
            or error.category not in SAFE_PROVIDER_ERROR_CATEGORIES):
        return None
    code = f"source_lookup_failed_{entity}_{method}_{field}_{error.category}"
    if error.transport_category is not None:
        transport = _safe_transport_category(error.transport_category) or "provider_transport_failed"
        code += f"_transport_{transport}"
    return code


def is_safe_diagnostic_code(code: object) -> bool:
    """Validate ordinary diagnostics and the structured lookup diagnostics."""
    if not isinstance(code, str):
        return False
    if _SIMPLE_DIAGNOSTIC.fullmatch(code):
        return True
    for entity in LOOKUP_ENTITIES:
        for method in LOOKUP_METHODS:
            fields = LOOKUP_FIELDS[entity] if method == "by_field" else frozenset({"id"})
            for field in fields:
                for category in SAFE_PROVIDER_ERROR_CATEGORIES:
                    prefix = f"source_lookup_failed_{entity}_{method}_{field}_{category}"
                    if code == prefix:
                        return True
                    transport_prefix = prefix + "_transport_"
                    if code.startswith(transport_prefix):
                        transport = code[len(transport_prefix):]
                        if transport == "provider_transport_failed" or _safe_transport_category(transport):
                            return True
    return False


def decode_canonical_key(entity: str, canonical_key: str) -> int:
    """Decode only the current one-column official integer `id` key format."""
    try:
        if entity not in snapshot_manifest.REQUIRED_ENTITIES:
            raise ValueError
        value = json.loads(canonical_key)
        if (not isinstance(value, dict) or set(value) != {"entity", "key"}
                or value["entity"] != entity or not isinstance(value["key"], list)
                or len(value["key"]) != 1):
            raise ValueError
        key = value["key"][0]
        if (not isinstance(key, list) or len(key) != 3 or key[0] != "id"
                or key[1] not in INTEGER_SQL_TYPES or not _is_int(key[2])):
            raise ValueError
        if json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != canonical_key:
            raise ValueError
        return key[2]
    except (TypeError, ValueError, json.JSONDecodeError):
        raise CandidateExtractionError("canonical_key_invalid") from None


def _aggregate_diff(report: Mapping[str, object]) -> dict[str, object]:
    entities = report.get("entities")
    totals = report.get("totals")
    if not isinstance(entities, Mapping) or not isinstance(totals, Mapping):
        raise CandidateExtractionError("manifest_diff_result_invalid")
    return {"entities": dict(entities), "totals": dict(totals)}


def _normalize_rows(entity: str, rows: Sequence[Mapping[str, object]], diagnostics: Counter[str]):
    result = normalize_many(list(rows), entity)
    diagnostics.update(item.code for item in result.diagnostics)
    return result.record or (), result.complete


def _safe_lookup(
    provider: FolketingetReadOnlyProvider,
    method: str,
    entity: str,
    ids: Sequence[int],
    diagnostics: Counter[str],
    *,
    field: str = "id",
) -> tuple[Mapping[str, object], ...]:
    try:
        if method == "by_ids":
            found = provider.get_by_ids(entity, tuple(ids))
            expected = set(ids)
            valid = tuple(row for row in found if isinstance(row, Mapping) and _is_int(row.get("id")) and row["id"] in expected)
            if len(valid) != len(found):
                diagnostics.update(("unexpected_source_rows",))
            return valid
        found = provider.find_by_field(entity, field, tuple(ids))
        expected = set(ids)
        valid = tuple(row for row in found if isinstance(row, Mapping) and _is_int(row.get(field)) and row[field] in expected)
        if len(valid) != len(found):
            diagnostics.update(("unexpected_source_rows",))
        return valid
    except SQLProviderError as error:
        diagnostics.update(("source_lookup_failed",))
        code = _lookup_failure_code(method, entity, field, error)
        if code is not None:
            diagnostics.update((code,))
        return ()
    except Exception:
        diagnostics.update(("source_lookup_failed",))
        return ()


def _required_int(row: Mapping[str, object], field: str) -> int | None:
    value = row.get(field)
    return value if _is_int(value) else None


def _resolve_affected_sag_ids(
    entity: str,
    record_id: int,
    provider: FolketingetReadOnlyProvider,
    diagnostics: Counter[str],
) -> set[int]:
    records = _safe_lookup(provider, "by_ids", entity, (record_id,), diagnostics)
    current = [row for row in records if row.get("id") == record_id]
    if not current:
        diagnostics.update(("changed_row_missing_from_new_source",))
        return set()
    row = current[0]
    if entity == "Sag":
        return {record_id}
    if entity in {"Sagstrin", "SagDokument"}:
        sag_id = _required_int(row, "sagid")
        if sag_id is None:
            diagnostics.update(("unresolved_relationship",))
            diagnostics.update(("sagstrin_invalid_sagid" if entity == "Sagstrin" else "sagdokument_invalid_sagid",))
            return set()
        return {sag_id}
    if entity == "Dokument":
        links = _safe_lookup(provider, "by_field", "SagDokument", (record_id,), diagnostics, field="dokumentid")
        sag_ids = {_required_int(link, "sagid") for link in links}
        if not links:
            diagnostics.update(("unresolved_relationship",))
            diagnostics.update(("document_no_links",))
        elif None in sag_ids:
            diagnostics.update(("unresolved_relationship",))
            diagnostics.update(("document_link_invalid_sagid",))
        return {value for value in sag_ids if value is not None}
    if entity == "Afstemning":
        stage_id = _required_int(row, "sagstrinid")
        if stage_id is None:
            diagnostics.update(("unresolved_relationship",))
            diagnostics.update(("vote_invalid_stageid",))
            return set()
        stages = _safe_lookup(provider, "by_ids", "Sagstrin", (stage_id,), diagnostics)
        sag_ids = {_required_int(stage, "sagid") for stage in stages}
        if not stages:
            diagnostics.update(("unresolved_relationship",))
            diagnostics.update(("vote_stage_not_found",))
        elif None in sag_ids:
            diagnostics.update(("unresolved_relationship",))
            diagnostics.update(("vote_stage_invalid_sagid",))
        return {value for value in sag_ids if value is not None}
    if entity == "Sagsstatus":
        sags = _safe_lookup(provider, "by_field", "Sag", (record_id,), diagnostics, field="statusid")
        sag_ids = {_required_int(sag, "id") for sag in sags}
        if not sags:
            diagnostics.update(("unresolved_relationship",))
            diagnostics.update(("status_no_matching_sags",))
        elif None in sag_ids:
            diagnostics.update(("unresolved_relationship",))
            diagnostics.update(("status_invalid_sag_id",))
        return {value for value in sag_ids if value is not None}
    diagnostics.update(("unsupported_entity",))
    return set()


def _resolve_removed_sag_ids(entity: str, record_id: int, routing: snapshot_diff.ManifestRoutingReader) -> set[int]:
    """Resolve old-side removals using only validated integer routing metadata."""
    if entity == "Sag":
        return {record_id}
    if entity in {"Sagstrin", "SagDokument"}:
        row = routing.get_row(entity, record_id)
        sagid = row.get("sagid") if row else None
        if not _is_int(sagid):
            raise snapshot_diff.DiffError("manifest_routing_unresolved")
        return {sagid}
    if entity == "Dokument":
        affected: set[int] = set()
        for row in routing.iter_by_field("SagDokument", "dokumentid", record_id):
            if _is_int(row.get("sagid")):
                affected.add(row["sagid"])
                if len(affected) > MAX_AFFECTED_CASES:
                    raise CandidateExtractionError("affected_case_limit_exceeded")
        return affected
    if entity == "Afstemning":
        vote = routing.get_row(entity, record_id)
        stage_id = vote.get("sagstrinid") if vote else None
        if not _is_int(stage_id):
            raise snapshot_diff.DiffError("manifest_routing_unresolved")
        stage_row = routing.get_row("Sagstrin", stage_id)
        sagid = stage_row.get("sagid") if stage_row else None
        if not _is_int(sagid):
            raise snapshot_diff.DiffError("manifest_routing_unresolved")
        return {sagid}
    if entity == "Sagsstatus":
        affected: set[int] = set()
        for row in routing.iter_by_field("Sag", "statusid", record_id):
            if _is_int(row.get("id")):
                affected.add(row["id"])
                if len(affected) > MAX_AFFECTED_CASES:
                    raise CandidateExtractionError("affected_case_limit_exceeded")
        return affected
    raise CandidateExtractionError("removed_entity_unsupported")


def _hydrate_cases(
    affected_ids: set[int], provider: FolketingetReadOnlyProvider, diagnostics: Counter[str]
) -> tuple[tuple[LegislativeCase, ...], int]:
    if not affected_ids:
        return (), 0
    ordered_ids = tuple(sorted(affected_ids))
    sag_raw = _safe_lookup(provider, "by_ids", "Sag", ordered_ids, diagnostics)
    sags, _ = _normalize_rows("Sag", sag_raw, diagnostics)
    found_sag_ids = {getattr(item, "id", None) for item in sags}
    if found_sag_ids != affected_ids:
        diagnostics.update(("affected_case_missing",))

    stage_raw = _safe_lookup(provider, "by_field", "Sagstrin", ordered_ids, diagnostics, field="sagid")
    link_raw = _safe_lookup(provider, "by_field", "SagDokument", ordered_ids, diagnostics, field="sagid")
    stages, _ = _normalize_rows("Sagstrin", stage_raw, diagnostics)
    links, _ = _normalize_rows("SagDokument", link_raw, diagnostics)
    document_ids = tuple(sorted({item.dokumentid for item in links if _is_int(getattr(item, "dokumentid", None))}))
    documents_raw = _safe_lookup(provider, "by_ids", "Dokument", document_ids, diagnostics) if document_ids else ()
    documents, _ = _normalize_rows("Dokument", documents_raw, diagnostics)
    stage_ids = tuple(sorted({item.id for item in stages if _is_int(getattr(item, "id", None))}))
    votes_raw = _safe_lookup(provider, "by_field", "Afstemning", stage_ids, diagnostics, field="sagstrinid") if stage_ids else ()
    votes, _ = _normalize_rows("Afstemning", votes_raw, diagnostics)
    status_ids = tuple(sorted({item.statusid for item in sags if _is_int(getattr(item, "statusid", None))}))
    statuses_raw = _safe_lookup(provider, "by_ids", "Sagsstatus", status_ids, diagnostics) if status_ids else ()
    statuses, _ = _normalize_rows("Sagsstatus", statuses_raw, diagnostics)

    assembled = assemble_cases(
        sags,
        stages=stages,
        document_links=links,
        documents=documents,
        votes=votes,
        statuses=statuses,
    )
    diagnostics.update(item.code for item in assembled.diagnostics)
    hydrated = sum(1 for case in assembled.cases if case.complete)
    return assembled.cases, hydrated


def extract_candidates(
    old_manifest: Path,
    new_manifest: Path,
    provider: FolketingetReadOnlyProvider,
    *,
    max_affected_cases: int = MAX_AFFECTED_CASES,
) -> dict[str, object]:
    if not _is_int(max_affected_cases) or max_affected_cases < 1 or max_affected_cases > MAX_AFFECTED_CASES:
        raise CandidateExtractionError("candidate_limit_invalid")
    try:
        diff_report = snapshot_diff.compare_manifests(old_manifest, new_manifest)
    except snapshot_diff.DiffError as error:
        raise CandidateExtractionError(error.category) from None

    diff_counts = _aggregate_diff(diff_report)
    diagnostics: Counter[str] = Counter()
    affected_ids: set[int] = set()
    tombstone_ids: set[int] = set()
    routing: snapshot_diff.ManifestRoutingReader | None = None
    routing_unavailable = False
    try:
        changes = snapshot_diff._iter_manifest_changes_validated(
            snapshot_diff._manifest_file(old_manifest), snapshot_diff._manifest_file(new_manifest)
        )
        for entity, category, canonical_key in changes:
            if category in {"technical_only"}:
                continue
            if category == "removed":
                if routing is None and not routing_unavailable:
                    try:
                        routing = snapshot_diff.ManifestRoutingReader(snapshot_diff._manifest_file(old_manifest))
                    except snapshot_diff.DiffError as error:
                        if error.category == "manifest_routing_unavailable":
                            routing_unavailable = True
                        else:
                            raise
                if routing_unavailable:
                    diagnostics["removed_change_requires_old_snapshot_hydration"] += 1
                    continue
                record_id = decode_canonical_key(entity, canonical_key)
                try:
                    removed_ids = _resolve_removed_sag_ids(entity, record_id, routing)
                    # A removed Sag is an explicit source deletion, not a case to hydrate
                    # from the new snapshot. Relationship removals still hydrate normally.
                    if entity == "Sag":
                        tombstone_ids.update(removed_ids)
                    else:
                        affected_ids.update(removed_ids)
                except snapshot_diff.DiffError:
                    diagnostics["removed_relationship_unresolved"] += 1
                if len(affected_ids | tombstone_ids) > max_affected_cases:
                    raise CandidateExtractionError("affected_case_limit_exceeded")
                continue
            if category not in {"added", "changed"}:
                continue
            record_id = decode_canonical_key(entity, canonical_key)
            affected_ids.update(_resolve_affected_sag_ids(entity, record_id, provider, diagnostics))
            if len(affected_ids | tombstone_ids) > max_affected_cases:
                raise CandidateExtractionError("affected_case_limit_exceeded")
    except snapshot_diff.DiffError as error:
        raise CandidateExtractionError(error.category) from None
    finally:
        if routing is not None:
            routing.close()
    affected_case_count = len(affected_ids | tombstone_ids)
    # A deleted case may also be referenced by changed child rows in this diff.
    # The explicit Sag removal wins: emit only its tombstone, never a fake upsert.
    affected_ids.difference_update(tombstone_ids)
    cases, hydrated_count = _hydrate_cases(affected_ids, provider, diagnostics)
    payloads: list[dict[str, object]] = []
    for case in cases:
        payload = case.to_dict()
        payload.update({"historical": False, "notify_eligible": None})
        payloads.append(payload)
    payloads.sort(key=lambda item: str(item.get("external_id", "")))
    tombstones = [
        {"operation": "SOURCE_DELETE", "source": SOURCE, "external_id": str(identifier)}
        for identifier in sorted(tombstone_ids)
    ]
    complete = not diagnostics and all(case.complete for case in cases)
    return {
        "format_version": FORMAT_VERSION,
        "source": SOURCE,
        "diff": diff_counts,
        "candidate_case_count": len(payloads),
        "cases": payloads,
        "tombstones": tombstones,
        "diagnostics": [{"code": code, "count": count} for code, count in sorted(diagnostics.items())],
        "complete": complete,
        "_summary": {
            "affected_case_count": affected_case_count,
            "hydrated_case_count": hydrated_count,
            "tombstone_count": len(tombstones),
        },
    }


def candidate_summary(bundle: Mapping[str, object]) -> dict[str, object]:
    """Return a log-safe aggregate view; never includes keys, IDs, or payloads."""
    raw_diff = bundle.get("diff")
    diff = raw_diff if isinstance(raw_diff, Mapping) else {}
    summary = bundle.get("_summary")
    summary = summary if isinstance(summary, Mapping) else {}
    return {
        "diff": dict(diff),
        "affected_case_count": int(summary.get("affected_case_count", 0)),
        "hydrated_case_count": int(summary.get("hydrated_case_count", 0)),
        "tombstone_count": int(summary.get("tombstone_count", 0)),
        "diagnostics": list(bundle.get("diagnostics", ())),
        "complete": bundle.get("complete") is True,
    }


__all__ = [
    "CandidateExtractionError", "FolketingetReadOnlyProvider", "InMemoryFolketingetProvider",
    "MAX_AFFECTED_CASES", "candidate_summary", "decode_canonical_key", "extract_candidates",
]
