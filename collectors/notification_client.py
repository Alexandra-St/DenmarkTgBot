"""Scheduled-only, fail-closed bridge to the Worker notification endpoints.

This client makes at most one request to each endpoint. Worker flags and
activation cutoffs remain independent enforcement gates.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

Opener = Callable[..., Any]
REPLAY_CONFIRMATION = "REPLAY_COMMITTED_RUN_NOTIFICATIONS"


def _valid_run_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


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
    replay: bool = False,
    replay_confirmation: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"queue_status": "not_run", "queued": 0, "delivery_status": "not_run", "sent": 0, "failed": 0}
    if replay:
        if event_name != "workflow_dispatch" or replay_confirmation != REPLAY_CONFIRMATION:
            result.update(queue_status="blocked", delivery_status="not_run")
            raise OrchestrationError("notification_replay_confirmation_required", result)
        data = dict(ingestion) if isinstance(ingestion, Mapping) else {}
        run_id = data.get("run_id")
        snapshot_commit = (
            data.get("status") == "COMMITTED"
            and data.get("mode") == "incremental"
            and data.get("state_kind") == "snapshot"
            and data.get("snapshot_advanced") is True
        )
        if not snapshot_commit or not _valid_run_id(run_id):
            result.update(queue_status="blocked", delivery_status="not_run")
            raise OrchestrationError("ingestion_not_confirmed_committed", result)
    # Manual dry-runs and manual production imports never invoke notifications.
    elif event_name != "schedule":
        result.update(queue_status="manual_skipped", delivery_status="manual_skipped")
        return result
    if queue_gate != "true":
        result.update(queue_status="disabled", delivery_status="not_run")
        return result
    if not replay:
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
        if (not (checkpoint_commit or snapshot_commit) or not _valid_run_id(run_id)):
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

    sent_total = 0
    failed_total = 0
    remaining = 1
    delivery_requests = 0
    while remaining > 0 and delivery_requests < 20:
        delivery_requests += 1
        try:
            delivery = _request(endpoint.rstrip("/") + "/internal/notifications/deliver", token, {"run_id": run_id}, opener)
        except OrchestrationError as exc:
            result.update(delivery_status="failed", sent=sent_total, failed=failed_total)
            raise OrchestrationError(exc.category, result) from None
        if delivery.get("enabled") is not True:
            result.update(delivery_status="worker_gate_closed", sent=sent_total, failed=failed_total)
            return result
        sent, failed, remaining_batch = delivery.get("sent"), delivery.get("failed"), delivery.get("remaining")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (sent, failed, remaining_batch)):
            result.update(delivery_status="failed", sent=sent_total, failed=failed_total)
            raise OrchestrationError("delivery_response_invalid", result)
        sent_total += sent
        failed_total += failed
        remaining = remaining_batch
        result.update(sent=sent_total, failed=failed_total)
        if failed_total:
            result["delivery_status"] = "completed_with_failures"
            raise OrchestrationError("delivery_failed_or_ambiguous", result)
    if remaining > 0:
        result["delivery_status"] = "failed"
        raise OrchestrationError("delivery_batch_limit_exceeded", result)
    result["delivery_status"] = "completed"
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
    parser = argparse.ArgumentParser(description="Queue/deliver notifications after a committed ingestion, or explicitly replay one committed snapshot run.")
    parser.add_argument("ingestion", type=Path)
    parser.add_argument("--replay", action="store_true", help="explicitly replay notifications for one committed snapshot run")
    parser.add_argument("--confirm-replay", default=None)
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
            replay=args.replay,
            replay_confirmation=args.confirm_replay,
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
