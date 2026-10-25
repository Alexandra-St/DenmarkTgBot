"""Build an exact plan from a local read-only audit export; never contacts production."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

MAX_PLAN_BYTES = 61_440
MAX_ENTRIES = 1000


class PlanError(ValueError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlanError("audit_json_invalid")
        result[key] = value
    return result


def _reject_constant(_):
    raise PlanError("audit_json_invalid")


def parse_audit(raw: str) -> list[dict]:
    try:
        data = json.loads(raw, object_pairs_hook=_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError):
        raise PlanError("audit_json_invalid") from None
    if isinstance(data, dict) and set(data) == {"notifications"}:
        data = data["notifications"]
    elif isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) and "results" in data[0]:
        # A single SELECT from `wrangler d1 execute --json`, not a multi-query dump.
        data = data[0]["results"]
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise PlanError("audit_shape_invalid")
    return data


def _positive_id(value):
    return type(value) is int and 0 < value <= 9_007_199_254_740_991


def _flag(value):
    return type(value) in (int, bool) and value in (0, 1)


def build_plan(rows: list[dict]) -> tuple[dict, dict]:
    entries = []
    seen = set()
    counts = {"scanned": len(rows), "pending": 0, "irrelevant_count": 0, "preference_count": 0, "out_of_scope": 0}
    for row in rows:
        notification_id = row.get("notification_id")
        if not _positive_id(notification_id) or notification_id in seen:
            raise PlanError("audit_identity_invalid")
        seen.add(notification_id)
        if row.get("status") not in ("PENDING", "SENT", "SENDING", "FAILED"):
            raise PlanError("audit_status_invalid")
        if row["status"] != "PENDING":
            continue
        counts["pending"] += 1
        if (not _positive_id(row.get("event_id")) or row.get("source") not in ("folketinget", "hoeringsportalen")
                or not isinstance(row.get("external_id"), str) or not re.fullmatch(r"[1-9][0-9]{0,18}", row["external_id"])
                or not _flag(row.get("recipient_exists"))):
            raise PlanError("audit_identity_invalid")
        if not row["recipient_exists"]:
            counts["out_of_scope"] += 1
            continue
        if (row.get("relevance") not in ("DIRECT", "POTENTIAL", "IRRELEVANT")
                or not _flag(row.get("notify_direct")) or not _flag(row.get("notify_potential"))):
            raise PlanError("audit_eligibility_invalid")
        if row["relevance"] == "IRRELEVANT":
            reason = "reclassified_irrelevant"
            counts["irrelevant_count"] += 1
        elif not row["notify_direct" if row["relevance"] == "DIRECT" else "notify_potential"]:
            reason = "reclassified_not_eligible"
            counts["preference_count"] += 1
        else:
            # Age/cutoff/history/deletion/completeness cannot add a plan entry.
            counts["out_of_scope"] += 1
            continue
        entries.append({"notification_id": notification_id, "event_id": row["event_id"],
                        "source": row["source"], "external_id": row["external_id"],
                        "expected_status": "PENDING", "expected_reconciliation_reason": reason})
    if not entries:
        raise PlanError("no_reconciliation_candidates")
    if len(entries) > MAX_ENTRIES:
        raise PlanError("plan_count_exceeded")
    plan = {"entries": entries}
    size = len(json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > MAX_PLAN_BYTES:
        raise PlanError("PLAN_INPUT_TOO_LARGE")
    return plan, {**counts, "expected_count": len(entries), "plan_input_bytes": size}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-json", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        plan, summary = build_plan(parse_audit(args.audit_json.read_text(encoding="utf-8")))
        data = json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        except OSError:
            args.output.unlink(missing_ok=True)
            raise
        print(json.dumps(summary, sort_keys=True))
        return 0
    except PlanError as error:
        print("pending_reconciliation_plan=FAIL category=" + str(error))
    except (OSError, UnicodeError):
        print("pending_reconciliation_plan=FAIL category=audit_io_failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
