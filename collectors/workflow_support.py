"""Fail-closed mode gate and redacted GitHub Actions run summary."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from collectors.ingestion_client import collection_complete
from collectors.relevance import classify


class WorkflowGateError(ValueError):
    pass


def validate_execution_mode(event: str, manual_confirmation: str, automation_enabled: str) -> str:
    if event == "workflow_dispatch":
        if manual_confirmation.lower() != "true":
            raise WorkflowGateError("manual production confirmation is required")
        return "manual"
    if event == "schedule":
        if automation_enabled != "true":
            raise WorkflowGateError("scheduled automation is disabled or invalid")
        return "automated"
    raise WorkflowGateError("unsupported workflow event")


def _load(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).is_file() else None
    except (OSError, json.JSONDecodeError):
        return None


def render_summary(collection: Any, ingestion: Any, *, run_id: str, gate_status: str, collect_status: str, ingest_status: str) -> str:
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
    if isinstance(collection, dict) and collect_status == "success" and not collection_complete(collection, "hoeringsportalen"):
        collect_status = "incomplete"
    reported_ingestion = ingestion.get("status", ingest_status) if isinstance(ingestion, dict) else ingest_status
    rows = [
        "## Høringsportalen monitoring run",
        f"- GitHub run ID: `{run_id or 'unavailable'}`",
        f"- Safety gate: `{gate_status}`",
        f"- Collection: `{collect_status}`",
        *feed_lines,
        f"- Unique publications: **{unique_count}**",
        f"- Relevance: DIRECT {relevance.get('DIRECT', 0)}, POTENTIAL {relevance.get('POTENTIAL', 0)}, IRRELEVANT {relevance.get('IRRELEVANT', 0)}",
        f"- Ingestion: `{reported_ingestion}`",
    ]
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
    args = parser.parse_args(argv)
    if args.command == "gate":
        try:
            mode = validate_execution_mode(
                os.environ.get("GITHUB_EVENT_NAME", ""),
                os.environ.get("CONFIRM_PRODUCTION_INGESTION", "false"),
                os.environ.get("HOERINGSPORTAL_AUTOMATION_ENABLED", ""),
            )
        except WorkflowGateError as exc:
            print(f"workflow gate: {exc}", file=sys.stderr)
            return 2
        print(f"Execution mode: {mode}")
        output_path = os.environ.get("GITHUB_OUTPUT")
        if output_path:
            with Path(output_path).open("a", encoding="utf-8") as output:
                output.write(f"run_id={uuid.uuid4()}\n")
        return 0
    body = render_summary(
        _load(args.collection), _load(args.ingestion),
        run_id=os.environ.get("GITHUB_RUN_ID", ""),
        gate_status=os.environ.get("GATE_STATUS", "not_run"),
        collect_status=os.environ.get("COLLECT_STATUS", "not_run"),
        ingest_status=os.environ.get("INGEST_STATUS", "not_run"),
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
