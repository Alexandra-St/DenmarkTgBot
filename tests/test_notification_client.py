from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from collectors.notification_client import OrchestrationError, orchestrate
from collectors.workflow_support import render_summary


RUN_ID = "00000000-0000-4000-8000-000000000001"
COMMITTED = {"run_id": RUN_ID, "status": "COMMITTED", "checkpoints_advanced": True}
ROOT = Path(__file__).resolve().parents[1]


class Response:
    def __init__(self, value: dict[str, Any]):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.value).encode()


class NotificationClientTests(unittest.TestCase):
    def invoke(self, responses, *, ingestion=COMMITTED, event="schedule", queue="true", delivery="true"):
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            value = responses.pop(0)
            return Response(value)

        result = orchestrate(
            ingestion, event_name=event, queue_gate=queue, delivery_gate=delivery,
            endpoint="https://worker.example", token="test-token-never-logged", opener=opener,
        )
        return result, requests

    def test_committed_scheduled_run_queues_then_delivers_using_actual_run_id(self):
        result, requests = self.invoke([{"eligible": True, "queued": 3}, {"enabled": True, "sent": 3, "failed": 0}])
        self.assertEqual(result, {"queue_status": "eligible", "queued": 3, "delivery_status": "completed", "sent": 3, "failed": 0})
        self.assertEqual([request.full_url for request, _ in requests], [
            "https://worker.example/internal/notifications/prepare",
            "https://worker.example/internal/notifications/deliver",
        ])
        self.assertEqual(json.loads(requests[0][0].data), {"run_id": RUN_ID})
        self.assertTrue(all(timeout == 20 for _, timeout in requests))

    def test_manual_production_and_dry_run_never_call_notification_endpoints(self):
        for mode in ("dry_run", "production_ingest"):
            with self.subTest(mode=mode):
                result, requests = self.invoke([], event="workflow_dispatch")
                self.assertEqual(result["queue_status"], "manual_skipped")
                self.assertEqual(requests, [])

    def test_missing_queue_gate_skips_all_requests(self):
        result, requests = self.invoke([], queue="")
        self.assertEqual(result["queue_status"], "disabled")
        self.assertEqual(requests, [])

    def test_delivery_gate_is_independent_and_queue_worker_gate_must_open(self):
        result, requests = self.invoke([{"eligible": True, "queued": 0}], delivery="false")
        self.assertEqual(result["delivery_status"], "disabled")
        self.assertEqual(len(requests), 1)
        result, requests = self.invoke([{"eligible": False, "queued": 0}])
        self.assertEqual(result["delivery_status"], "not_run")
        self.assertEqual(len(requests), 1)

    def test_uncommitted_or_unadvanced_run_never_reaches_queue(self):
        for value in ({"run_id": RUN_ID, "status": "OPEN", "checkpoints_advanced": False},
                      {"run_id": RUN_ID, "status": "COMMITTED", "checkpoints_advanced": False}):
            with self.subTest(value=value):
                requests = []
                with self.assertRaises(OrchestrationError) as caught:
                    orchestrate(value, event_name="schedule", queue_gate="true", delivery_gate="true",
                                endpoint="https://worker.example", token="x",
                                opener=lambda *_args, **_kwargs: requests.append(True))
                self.assertEqual(caught.exception.category, "ingestion_not_confirmed_committed")
                self.assertEqual(requests, [])

    def test_queue_failure_blocks_delivery_and_delivery_is_never_retried(self):
        calls = []

        def bad_queue(request, timeout):
            calls.append(request.full_url)
            raise OSError("secret and response body must not leak")

        with self.assertRaises(OrchestrationError) as caught:
            orchestrate(COMMITTED, event_name="schedule", queue_gate="true", delivery_gate="true",
                        endpoint="https://worker.example", token="hidden", opener=bad_queue)
        self.assertEqual(caught.exception.category, "notification_request_failed")
        self.assertEqual(calls, ["https://worker.example/internal/notifications/prepare"])

        calls.clear()
        def bad_delivery(request, timeout):
            calls.append(request.full_url)
            if request.full_url.endswith("prepare"):
                return Response({"eligible": True, "queued": 1})
            raise TimeoutError("ambiguous")

        with self.assertRaises(OrchestrationError):
            orchestrate(COMMITTED, event_name="schedule", queue_gate="true", delivery_gate="true",
                        endpoint="https://worker.example", token="hidden", opener=bad_delivery)
        self.assertEqual(calls, ["https://worker.example/internal/notifications/prepare", "https://worker.example/internal/notifications/deliver"])

    def test_duplicate_invocations_reuse_committed_run_id_for_worker_idempotency(self):
        sent_run_ids = []

        def opener(request, timeout):
            if request.full_url.endswith("prepare"):
                sent_run_ids.append(json.loads(request.data)["run_id"])
                return Response({"eligible": True, "queued": 0})
            return Response({"enabled": True, "sent": 0, "failed": 0})

        for _ in range(2):
            orchestrate(COMMITTED, event_name="schedule", queue_gate="true", delivery_gate="true",
                        endpoint="https://worker.example", token="hidden", opener=opener)
        self.assertEqual(sent_run_ids, [RUN_ID, RUN_ID])

    def test_workflow_only_invokes_orchestration_after_scheduled_production_commit(self):
        workflow = (ROOT / ".github/workflows/hoeringsportalen-incremental.yml").read_text(encoding="utf-8")
        step = workflow.split("- name: Queue and deliver scheduled notifications", 1)[1].split("- name: Write safe run summary", 1)[0]
        self.assertIn("github.event_name == 'schedule' && steps.production_ingest.outcome == 'success'", step)
        self.assertIn("NOTIFICATION_QUEUE_AUTOMATION_ENABLED: ${{ vars.NOTIFICATION_QUEUE_AUTOMATION_ENABLED || 'DISABLED' }}", step)
        self.assertIn("NOTIFICATION_DELIVERY_AUTOMATION_ENABLED: ${{ vars.NOTIFICATION_DELIVERY_AUTOMATION_ENABLED || 'DISABLED' }}", step)
        self.assertIn("--notifications", workflow)

    def test_summary_includes_only_safe_aggregate_notification_state(self):
        rendered = render_summary({}, COMMITTED, run_id="123", mode="production_ingest", gate_status="success",
                                  collect_status="success", ingest_status="success",
                                  notifications={"queue_status": "eligible", "queued": 2,
                                                 "delivery_status": "completed", "sent": 2,
                                                 "failed": 0, "error_category": "queue_failed",
                                                 "telegram_id": 99, "token": "secret"},
                                  notification_status="success")
        self.assertIn("Queue: `eligible` (queued 2)", rendered)
        self.assertNotIn("telegram_id", rendered)
        self.assertNotIn("secret", rendered)


if __name__ == "__main__":
    unittest.main()
