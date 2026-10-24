"""Scheduled-only, fail-closed bridge to the Worker notification endpoints.

This client makes at most one request to each endpoint. Worker flags and
activation cutoffs remain independent enforcement gates.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

Opener = Callable[..., Any]
_RUN_ID = re.compile(r"^[0-9a-f-]{36}$", re.IGNORECASE)


class OrchestrationError(ValueError):
    def __init__(self, category: str, result: dict[str, Any]):
        super().__init__(category)
        self.category = category
        self.result = result


def _request(endpoint: str, token: str, payload: object, opener: Opener) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Authorization": "Bearer " + token, "Accept": "application/json", "Content-Type": "application/json", "User-Agent": "DenmarkTgBot-NotificationOrchestrator/1.0"},
    )
    try:
        with opener(request, timeout=20) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError):
        raise OrchestrationError("notification_request_failed", {}) from None
    except urllib.error.HTTPError:
        raise OrchestrationError("notification_request_rejected", {}) from None
    if not isinstance(value, dict):
        raise OrchestrationError("notification_response_invalid", {})
    return value


def orchestrate(
    ingestion: object,
    *,
    event_name: str,
    queue_gate: str,
    delivery_gate: str,
    endpoint: str,
    token: str,
    opener: Opener = urllib.request.urlopen,
) -> dict[str, Any]:
    result: dict[str, Any] = {"queue_status": "not_run", "queued": 0, "delivery_status": "not_run", "sent": 0, "failed": 0}
    # Manual dry-runs and manual production imports never invoke notifications.
    if event_name != "schedule":
        result.update(queue_status="manual_skipped", delivery_status="manual_skipped")
        return result
    if queue_gate != "true":
        result.update(queue_status="disabled", delivery_status="not_run")
        return result
    data = dict(ingestion) if isinstance(ingestion, Mapping) else {}
    run_id = data.get("run_id")
    checkpoint_commit = (
        data.get("status") == "COMMITTED"
        and data.get("checkpoints_advanced") is True
        and data.get("state_kind") != "snapshot"
    )
    snapshot_commit = (
        data.get("status") == "COMMITTED"
        and data.get("mode") == "incremental"
        and data.get("state_kind") == "snapshot"
        and data.get("snapshot_advanced") is True
    )
    if (not (checkpoint_commit or snapshot_commit)
            or not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id)):
        result.update(queue_status="blocked", delivery_status="not_run")
        raise OrchestrationError("ingestion_not_confirmed_committed", result)
    if not endpoint.startswith("https://") or not token:
        result.update(queue_status="blocked", delivery_status="not_run")
        raise OrchestrationError("notification_client_not_configured", result)

    try:
        queue = _request(endpoint.rstrip("/") + "/internal/notifications/prepare", token, {"run_id": run_id}, opener)
    except OrchestrationError as exc:
        result.update(queue_status="failed", delivery_status="not_run")
        raise OrchestrationError(exc.category, result) from None
    if not isinstance(queue.get("eligible"), bool) or isinstance(queue.get("queued"), bool) or not isinstance(queue.get("queued"), int) or queue["queued"] < 0:
        result.update(queue_status="failed", delivery_status="not_run")
        raise OrchestrationError("queue_response_invalid", result)
    result.update(queue_status="eligible" if queue["eligible"] else "worker_gate_closed", queued=queue["queued"])
    if not queue["eligible"]:
        result["delivery_status"] = "not_run"
        return result
    if delivery_gate != "true":
        result["delivery_status"] = "disabled"
        return result

    try:
        delivery = _request(endpoint.rstrip("/") + "/internal/notifications/deliver", token, {}, opener)
    except OrchestrationError as exc:
        result["delivery_status"] = "failed"
        raise OrchestrationError(exc.category, result) from None
    if delivery.get("enabled") is not True:
        result["delivery_status"] = "worker_gate_closed"
        return result
    sent, failed = delivery.get("sent"), delivery.get("failed")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (sent, failed)):
        result["delivery_status"] = "failed"
        raise OrchestrationError("delivery_response_invalid", result)
    result.update(delivery_status="completed_with_failures" if failed else "completed", sent=sent, failed=failed)
    if failed:
        raise OrchestrationError("delivery_failed_or_ambiguous", result)
    return result


def main(argv: list[str] | None = None, opener: Opener = urllib.request.urlopen) -> int:
    parser = argparse.ArgumentParser(description="Queue/deliver notifications after a committed scheduled ingestion only.")
    parser.add_argument("ingestion", type=Path)
    args = parser.parse_args(argv)
    result: dict[str, Any] = {}
    try:
        ingestion = json.loads(args.ingestion.read_text(encoding="utf-8"))
        result = orchestrate(
            ingestion,
            event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
            queue_gate=os.environ.get("NOTIFICATION_QUEUE_AUTOMATION_ENABLED", ""),
            delivery_gate=os.environ.get("NOTIFICATION_DELIVERY_AUTOMATION_ENABLED", ""),
            endpoint=os.environ.get("INGEST_API_URL", ""),
            token=os.environ.get("INGEST_API_TOKEN", ""),
            opener=opener,
        )
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except (OSError, json.JSONDecodeError):
        result = {"queue_status": "blocked", "queued": 0, "delivery_status": "not_run", "sent": 0, "failed": 0}
        category = "ingestion_result_unavailable"
    except OrchestrationError as exc:
        result = exc.result or {"queue_status": "failed", "queued": 0, "delivery_status": "not_run", "sent": 0, "failed": 0}
        category = exc.category
    print(json.dumps({**result, "error_category": category}, separators=(",", ":")))
    print("notification orchestration failed: " + category, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
