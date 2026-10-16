"""Fail-closed mode gate and redacted GitHub Actions run summary."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from collectors.ingestion_client import collection_complete
from collectors.relevance import classify


class WorkflowGateError(ValueError):
    pass


def validate_execution_mode(event: str, requested_mode: str, manual_confirmation: str, automation_enabled: str) -> str:
    if event == "workflow_dispatch":
        if requested_mode == "dry_run":
            return "dry_run"
        if requested_mode == "production_ingest":
            if manual_confirmation != "true":
                raise WorkflowGateError("production ingestion confirmation is required")
            return "production_ingest"
        raise WorkflowGateError("unsupported manual execution mode")
    if event == "schedule":
        if automation_enabled != "true":
            raise WorkflowGateError("scheduled automation is disabled or invalid")
        return "production_ingest"
    raise WorkflowGateError("unsupported workflow event")


def _load(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).is_file() else None
    except (OSError, json.JSONDecodeError):
        return None


def render_summary(collection: Any, ingestion: Any, *, run_id: str, mode: str, gate_status: str, collect_status: str, ingest_status: str, notifications: Any = None, notification_status: str = "not_run") -> str:
    feeds = collection.get("feeds", {}) if isinstance(collection, dict) else {}
    feed_lines = []
    for feed in ("immigration_ministry", "immigration_category"):
        item = feeds.get(feed, {}) if isinstance(feeds, dict) else {}
        status = item.get("status", "unavailable") if isinstance(item, dict) else "unavailable"
        count = item.get("entries_parsed", 0) if isinstance(item, dict) else 0
        feed_lines.append(f"- `{feed}`: `{status}` ({count} entries)")
    publications = collection.get("publications", []) if isinstance(collection, dict) else []
    unique_count = len(publications) if isinstance(publications, list) else 0
    relevance: dict[str, int] = {}
    if isinstance(ingestion, dict) and isinstance(ingestion.get("relevance_counts"), dict):
        relevance = {str(k): int(v) for k, v in ingestion["relevance_counts"].items() if isinstance(v, int) and v >= 0}
    elif isinstance(publications, list):
        for item in publications:
            if isinstance(item, dict):
                category = classify(item).to_dict().get("relevance")
                if isinstance(category, str):
                    relevance[category] = relevance.get(category, 0) + 1
    complete = collection_complete(collection, "hoeringsportalen") if isinstance(collection, dict) else False
    if isinstance(collection, dict) and collect_status == "success" and not complete:
        collect_status = "incomplete"
    if mode == "dry_run" and isinstance(ingestion, dict):
        reported_ingestion = "DRY_RUN" if ingestion.get("would_submit") is True else "DRY_RUN_FAILED"
    elif mode == "dry_run":
        reported_ingestion = "DRY_RUN_FAILED" if ingest_status == "failure" or collect_status in {"failure", "incomplete"} else "not_run"
    else:
        reported_ingestion = ingestion.get("status", ingest_status) if isinstance(ingestion, dict) else ingest_status
    rows = [
        "## Høringsportalen monitoring run",
        f"- GitHub run ID: `{run_id or 'unavailable'}`",
        f"- Safety gate: `{gate_status}`",
        f"- Mode: `{mode or 'unknown'}`",
        f"- Collection: `{collect_status}`",
        f"- Collection complete: `{str(complete).lower()}`",
        *feed_lines,
        f"- Unique publications: **{unique_count}**",
        f"- Relevance: DIRECT {relevance.get('DIRECT', 0)}, POTENTIAL {relevance.get('POTENTIAL', 0)}, IRRELEVANT {relevance.get('IRRELEVANT', 0)}",
        f"- Ingestion: `{reported_ingestion}`",
    ]
    if isinstance(notifications, dict):
        # Only emit allowlisted aggregate fields; never copy arbitrary response data.
        allowed_statuses = {"disabled", "manual_skipped", "not_run", "blocked", "failed", "eligible", "worker_gate_closed", "disabled", "completed", "completed_with_failures"}
        queue_status = notifications.get("queue_status")
        delivery_status = notifications.get("delivery_status")
        safe_queue = queue_status if queue_status in allowed_statuses else "unknown"
        safe_delivery = delivery_status if delivery_status in allowed_statuses else "unknown"
        queued = notifications.get("queued", 0)
        sent = notifications.get("sent", 0)
        failed = notifications.get("failed", 0)
        rows.extend([
            f"- Notification orchestration: `{notification_status}`",
            f"- Queue: `{safe_queue}` (queued {queued if isinstance(queued, int) and queued >= 0 else 0})",
            f"- Delivery: `{safe_delivery}` (sent {sent if isinstance(sent, int) and sent >= 0 else 0}, failed {failed if isinstance(failed, int) and failed >= 0 else 0})",
        ])
        category = notifications.get("error_category")
        if isinstance(category, str) and re.fullmatch(r"[a-z_]{1,60}", category):
            rows.append(f"- Notification error category: `{category}`")
    if mode == "dry_run":
        rows.append("- Production write: **No** (dry-run mode does not call the Worker)")
    if isinstance(ingestion, dict) and isinstance(ingestion.get("run_id"), str):
        rows.append(f"- Ingestion run ID: `{ingestion['run_id']}`")
    if gate_status == "failure":
        rows.append("- Error category: `safety_gate_rejected` (details are withheld from the summary)")
    elif ingest_status == "failure" and collect_status == "success":
        rows.append("- Error category: `ingestion_failed_or_not_committed` (details are withheld from the summary)")
    elif collect_status in {"failure", "incomplete"}:
        rows.append("- Error category: `collection_failed_or_incomplete` (details are withheld from the summary)")
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gate")
    summary = sub.add_parser("summary")
    summary.add_argument("--collection", default="")
    summary.add_argument("--ingestion", default="")
    summary.add_argument("--notifications", default="")
    args = parser.parse_args(argv)
    if args.command == "gate":
        try:
            mode = validate_execution_mode(
                os.environ.get("GITHUB_EVENT_NAME", ""),
                os.environ.get("REQUESTED_MODE", "dry_run"),
                os.environ.get("CONFIRM_PRODUCTION_INGESTION", "false"),
                os.environ.get("HOERINGSPORTALEN_AUTOMATION_ENABLED", ""),
            )
        except WorkflowGateError as exc:
            print(f"workflow gate: {exc}", file=sys.stderr)
            return 2
        print(f"Execution mode: {mode}")
        output_path = os.environ.get("GITHUB_OUTPUT")
        if output_path:
            with Path(output_path).open("a", encoding="utf-8") as output:
                output.write(f"run_id={uuid.uuid4()}\n")
                output.write(f"mode={mode}\n")
        return 0
    body = render_summary(
        _load(args.collection), _load(args.ingestion),
        run_id=os.environ.get("GITHUB_RUN_ID", ""),
        mode=os.environ.get("RUN_MODE", ""),
        gate_status=os.environ.get("GATE_STATUS", "not_run"),
        collect_status=os.environ.get("COLLECT_STATUS", "not_run"),
        ingest_status=os.environ.get("INGEST_STATUS", "not_run"),
        notifications=_load(args.notifications),
        notification_status=os.environ.get("NOTIFICATION_STATUS", "not_run"),
    )
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as output:
            output.write(body)
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
