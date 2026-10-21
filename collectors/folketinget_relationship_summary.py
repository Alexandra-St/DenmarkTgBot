"""Deterministic compact semantic summaries for Folketinget relationships."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

SEMANTIC_FIELDS = {
    "documents": ("id", "link_id", "title", "date_raw", "released_at_raw", "document_released_at_raw"),
    "stages": ("id", "title", "date_raw", "type_id", "status_id"),
    "votes": ("id", "stage_id", "number", "approved", "conclusion", "comment"),
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def relationship_summary(rows: object, kind: str) -> dict[str, object]:
    fields = SEMANTIC_FIELDS.get(kind)
    if fields is None or not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("relationship_summary_invalid")
    semantic = [{field: row[field] for field in fields if field in row} for row in rows]
    semantic.sort(key=_canonical)
    digest = hashlib.sha256(_canonical(semantic).encode("utf-8")).hexdigest()
    return {"count": len(semantic), "sha256": digest}


def validate_relationship_summaries(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, Mapping) or set(value) != set(SEMANTIC_FIELDS):
        raise ValueError("relationship_summary_invalid")
    result: dict[str, dict[str, object]] = {}
    for kind in SEMANTIC_FIELDS:
        item = value.get(kind)
        if not isinstance(item, Mapping) or set(item) != {"count", "sha256"}:
            raise ValueError("relationship_summary_invalid")
        count = item.get("count")
        digest = item.get("sha256")
        if (isinstance(count, bool) or not isinstance(count, int) or count < 0
                or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
            raise ValueError("relationship_summary_invalid")
        result[kind] = {"count": count, "sha256": digest}
    return result


def compact_folketinget_record(record: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(record)
    snapshot = result.get("normalized_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("relationship_summary_invalid")
    compact_snapshot = dict(snapshot)
    if "relationship_summary" in compact_snapshot:
        if any(kind in compact_snapshot for kind in SEMANTIC_FIELDS):
            raise ValueError("relationship_summary_invalid")
        summaries = validate_relationship_summaries(compact_snapshot["relationship_summary"])
    else:
        summaries = {
            kind: relationship_summary(compact_snapshot.get(kind), kind)
            for kind in SEMANTIC_FIELDS
        }
    for kind in SEMANTIC_FIELDS:
        compact_snapshot.pop(kind, None)
    compact_snapshot["relationship_summary"] = summaries
    result["normalized_snapshot"] = compact_snapshot
    return result
