"""One-run incremental and historical Folketinget collection orchestration."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from collectors.folketinget import COLLECTIONS, Cursor, ODataClient, ODataError, ODataPageError, ODataPaginationError, ScanResult
from collectors.folketinget_models import Diagnostic
from collectors.folketinget_relations import load_case

_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,7})?$")
@dataclass(frozen=True)
class StreamResult:
    status: str
    pages: int
    records: int
    complete: bool
    upper_bound: dict[str, object] | None


def parse_checkpoints(value: object) -> dict[str, Cursor]:
    if not isinstance(value, dict):
        raise ValueError("checkpoints must be a JSON object")
    parsed: dict[str, Cursor] = {}
    for name, raw in value.items():
        if name not in COLLECTIONS or not isinstance(raw, dict):
            raise ValueError("checkpoint entries must use official stream names and objects")
        timestamp, entity_id = raw.get("timestamp"), raw.get("id")
        if not isinstance(timestamp, str) or not _STAMP.fullmatch(timestamp):
            raise ValueError(f"invalid timestamp in checkpoint for {name}")
        if isinstance(entity_id, bool) or not isinstance(entity_id, int) or entity_id < 0:
            raise ValueError(f"invalid id in checkpoint for {name}")
        parsed[name] = Cursor(timestamp, entity_id)
    return parsed


def _floor(days: int, overlap_hours: int) -> str:
    # ODA's timezone is undocumented. Widen the boundary by three days and
    # retain a source-style naive timestamp; never label it as source UTC.
    value = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days, hours=overlap_hours)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f")


def _scan(client: ODataClient, name: str, floor: str, max_pages: int, precision_retries: int) -> tuple[ScanResult | None, list[dict[str, object]], str]:
    expr = f"opdateringsdato ge datetime'{floor}'"
    attempts = 0
    partial: list[dict[str, object]] = []
    while True:
        try:
            result = client.scan(name, filter=expr, max_pages=max_pages)
            # A restarted, complete snapshot is authoritative. Partial pages
            # from a precision-conflicted attempt are deliberately discarded.
            status = "complete_after_precision_retry" if attempts else "complete"
            return result, list(result.records), status
        except ODataPageError as exc:
            partial.extend(exc.partial_result.records)
            precision_conflict = isinstance(exc.cause, ODataPaginationError) and any(
                marker in str(exc.cause)
                for marker in ("cursor did not advance", "exceeded the captured upper bound")
            )
            if precision_conflict and attempts < precision_retries:
                attempts += 1
                continue
            return None, partial, f"incomplete: {type(exc.cause).__name__ if exc.cause else 'page_error'}"
        except ODataError as exc:
            return None, partial, f"failed: {type(exc).__name__}"


def _collect_refs(client: ODataClient, entity: str, ids: list[int], field: str) -> tuple[list[dict[str, object]], bool]:
    rows: list[dict[str, object]] = []
    complete = True
    for offset in range(0, len(ids), 40):
        subset = ids[offset:offset + 40]
        if not subset:
            continue
        expression = " or ".join(f"{field} eq {item}" for item in subset)
        try:
            result = client.scan(entity, filter=f"({expression})", max_pages=100)
            rows.extend(result.records)
            complete = complete and result.metadata.complete
        except ODataError:
            complete = False
    return rows, complete


def _affected_cases(client: ODataClient, changes: Mapping[str, tuple[dict[str, object], ...]]) -> tuple[set[int], list[dict[str, object]], bool]:
    cases: set[int] = set()
    diagnostics: list[dict[str, object]] = []
    complete = True
    for row in changes.get("Sag", ()):
        if isinstance(row.get("id"), int): cases.add(int(row["id"]))
    for entity in ("Sagstrin", "SagDokument"):
        for row in changes.get(entity, ()):
            if isinstance(row.get("sagid"), int): cases.add(int(row["sagid"]))
            else: diagnostics.append({**asdict(Diagnostic("unresolved_case_reference", entity, "sagid is missing or null", row.get("id") if isinstance(row.get("id"), int) else None)), "blocking": True})
    doc_ids = sorted({int(row["id"]) for row in changes.get("Dokument", ()) if isinstance(row.get("id"), int)})
    linked, ok = _collect_refs(client, "SagDokument", doc_ids, "dokumentid")
    complete = complete and ok
    if not ok:
        diagnostics.append({**asdict(Diagnostic("relationship_resolution_failed", "SagDokument", "document-to-case reverse lookup failed")), "blocking": True})
    cases.update(int(row["sagid"]) for row in linked if isinstance(row.get("sagid"), int))
    stage_ids = sorted({int(row["sagstrinid"]) for row in changes.get("Afstemning", ()) if isinstance(row.get("sagstrinid"), int)})
    if any(row.get("sagstrinid") is None for row in changes.get("Afstemning", ())):
        diagnostics.append({**asdict(Diagnostic("unresolved_vote", "Afstemning", "vote has null sagstrinid; case cannot be identified")), "blocking": False})
    stages, ok = _collect_refs(client, "Sagstrin", stage_ids, "id")
    complete = complete and ok
    if not ok:
        diagnostics.append({**asdict(Diagnostic("relationship_resolution_failed", "Sagstrin", "vote-to-case stage lookup failed")), "blocking": True})
    cases.update(int(row["sagid"]) for row in stages if isinstance(row.get("sagid"), int))
    status_ids = sorted({int(row["id"]) for row in changes.get("Sagsstatus", ()) if isinstance(row.get("id"), int)})
    sags, ok = _collect_refs(client, "Sag", status_ids, "statusid")
    complete = complete and ok
    if not ok:
        diagnostics.append({**asdict(Diagnostic("relationship_resolution_failed", "Sag", "status-to-case lookup failed")), "blocking": True})
    cases.update(int(row["id"]) for row in sags if isinstance(row.get("id"), int))
    return cases, diagnostics, complete


def collect(client: ODataClient, *, mode: str, checkpoints: Mapping[str, Cursor] | None = None, days: int = 30, overlap_hours: int = 72, max_pages: int = 1000, precision_retries: int = 1) -> dict[str, object]:
    if mode not in {"historical", "incremental"}:
        raise ValueError("mode must be historical or incremental")
    if days < 1 or overlap_hours < 0 or max_pages < 1 or precision_retries < 0 or precision_retries > 2:
        raise ValueError("invalid collection limits")
    supplied = checkpoints or {}
    streams: dict[str, StreamResult] = {}
    changes: dict[str, tuple[dict[str, object], ...]] = {}
    diagnostics: list[dict[str, object]] = []
    ok_all = True
    for name in sorted(COLLECTIONS):
        cp = supplied.get(name)
        fallback_days = days if mode == "historical" or cp is None else 0
        floor = _floor(fallback_days, overlap_hours)
        if cp is not None:
            # Subtract overlap from the source timestamp's whole-second part;
            # preserve its original fractional precision verbatim.
            base, dot, fraction = cp.timestamp.partition(".")
            stamp = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S") - timedelta(hours=overlap_hours)
            floor = stamp.strftime("%Y-%m-%dT%H:%M:%S") + (dot + fraction if dot else "")
        result, rows, status = _scan(client, name, floor, max_pages, precision_retries)
        stream_complete = result is not None
        ok_all = ok_all and stream_complete
        changes[name] = tuple(rows)
        upper = result.metadata.upper_bound if result else None
        upper_payload = {"timestamp": upper.timestamp, "id": upper.entity_id} if upper else None
        streams[name] = StreamResult(status, result.metadata.page_count if result else 0, len(rows), stream_complete, upper_payload)
        if not stream_complete:
            diagnostics.append({**asdict(Diagnostic("stream_scan_incomplete", name, status)), "blocking": True})
        elif status == "complete_after_precision_retry":
            diagnostics.append({**asdict(Diagnostic("precision_retry_recovered", name, "discarded partial attempt; restarted scan completed")), "blocking": False})
    affected, relation_diagnostics, relations_complete = _affected_cases(client, changes)
    diagnostics.extend(relation_diagnostics)
    ok_all = ok_all and relations_complete
    cases: list[dict[str, object]] = []
    for case_id in sorted(affected):
        hydrated = load_case(client, case_id)
        if hydrated.case is not None:
            payload = hydrated.case.to_dict()
            payload["historical"] = mode == "historical"
            payload["notify_eligible"] = False if mode == "historical" else None
            cases.append(payload)
        diagnostics.extend({**asdict(item), "blocking": True} for item in hydrated.diagnostics)
        ok_all = ok_all and hydrated.complete
    return {
        "mode": mode,
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "historical": mode == "historical",
        "cases": cases,
        "streams": {name: asdict(streams[name]) for name in sorted(streams)},
        "committable_checkpoints": {},
        "checkpoint_note": "Checkpoints require successful durable persistence; this collector does not persist.",
        "diagnostics": diagnostics,
        "complete": ok_all and not any(item.get("blocking", True) for item in diagnostics),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Read-only Folketinget collection to JSON")
    parser.add_argument("mode", choices=("historical", "incremental"))
    parser.add_argument("--checkpoints", help="JSON checkpoint file (incremental mode)")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--overlap-hours", type=int, default=72)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        checkpoints: dict[str, Cursor] = {}
        if args.checkpoints:
            with open(args.checkpoints, encoding="utf-8") as file:
                checkpoints = parse_checkpoints(json.load(file))
        if args.mode == "incremental" and not args.checkpoints:
            raise ValueError("incremental mode requires --checkpoints")
        client = ODataClient(page_size=args.page_size)
        result = collect(client, mode=args.mode, checkpoints=checkpoints, days=args.days, overlap_hours=args.overlap_hours, max_pages=args.max_pages)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["complete"] else 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"folketinget collector: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
