from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from tools import pending_reconciliation_plan as generator
from tools import reclassification_workflow as transport
from tests.test_reclassification_workflow import Opener, Response

TOKEN = "private-bearer-token"
ROW = {"notification_id": 2, "event_id": 1743, "source": "hoeringsportalen", "external_id": "70190",
       "status": "PENDING", "relevance": "IRRELEVANT", "recipient_exists": 1,
       "notify_direct": 1, "notify_potential": 1, "telegram_id": 987654321,
       "relevance_reason": "PRIVATE_REASON", "snapshot_sha256": "e" * 64}
PLAN = '{ "entries": ' + json.dumps(generator.build_plan([ROW])[0]["entries"]) + ' }'
SUMMARY = {"status": "PREVIEW", "expected_count": 1, "ready": 1, "already_reconciled": 0,
           "stale": 0, "missing": 0, "invalid": 0, "blocked": 0, "writes": 0,
           "irrelevant_count": 1, "preference_count": 0, "plan_sha256": "a" * 64,
           "entries": [{"notification_id": 2, "external_id": "70190", "reason": "PRIVATE_REASON"}],
           "telegram_id": 987654321, "token": TOKEN}


class PendingPlanTests(unittest.TestCase):
    def test_exact_plan_omits_user_data_and_has_no_id_specific_selection(self):
        rows = [{**ROW, "notification_id": 41, "event_id": 2001, "source": "folketinget", "external_id": "12345"},
                {**ROW, "notification_id": 42, "relevance": "POTENTIAL", "notify_potential": 0}]
        plan, summary = generator.build_plan(rows)
        self.assertEqual(summary["expected_count"], 2)
        self.assertEqual(summary["irrelevant_count"], 1)
        self.assertEqual(summary["preference_count"], 1)
        self.assertEqual(plan["entries"][0]["notification_id"], 41)
        self.assertEqual(plan["entries"][1]["expected_reconciliation_reason"], "reclassified_not_eligible")
        expected = {"notification_id", "event_id", "source", "external_id", "expected_status", "expected_reconciliation_reason"}
        for entry in plan["entries"]:
            self.assertEqual(set(entry), expected)
        for private in ("telegram_id", "987654321", "PRIVATE_REASON", "e" * 64):
            self.assertNotIn(private, json.dumps(plan))

    def test_scan_excludes_terminal_eligible_missing_user_and_other_suppression_only(self):
        rows = [ROW, {**ROW, "notification_id": 5, "status": "SENT"},
                {**ROW, "notification_id": 6, "status": "SENDING"},
                {**ROW, "notification_id": 7, "status": "FAILED"},
                {**ROW, "notification_id": 8, "recipient_exists": 0},
                {**ROW, "notification_id": 9, "relevance": "POTENTIAL", "source_complete": 0,
                 "historical": 1, "source_deleted": 1, "notify_eligible": 0, "detected_at": "2000-01-01"}]
        plan, summary = generator.build_plan(rows)
        self.assertEqual(len(plan["entries"]), 1)
        self.assertEqual(summary["pending"], 3)
        self.assertEqual(summary["out_of_scope"], 2)
        for relevance, flag in (("DIRECT", "notify_direct"), ("POTENTIAL", "notify_potential")):
            with self.subTest(relevance=relevance):
                p, _ = generator.build_plan([{**ROW, "relevance": relevance, flag: 0}])
                self.assertEqual(p["entries"][0]["expected_reconciliation_reason"], "reclassified_not_eligible")
                with self.assertRaisesRegex(generator.PlanError, "no_reconciliation_candidates"):
                    generator.build_plan([{**ROW, "relevance": relevance, flag: 1}])

    def test_read_only_audit_formats_and_duplicate_keys(self):
        for raw in (json.dumps([ROW]), json.dumps({"notifications": [ROW]}), json.dumps([{"results": [ROW], "success": True}])):
            self.assertEqual(generator.parse_audit(raw), [ROW])
        for raw in ('{"notifications":[],"notifications":[]}', '{"notifications":[NaN]}', 'null', 'not json'):
            with self.subTest(raw=raw), self.assertRaises(generator.PlanError):
                generator.parse_audit(raw)

    def test_malformed_or_duplicate_identity_fails_closed(self):
        for extra in ({"notification_id": True}, {"event_id": 0}, {"source": "other"}, {"external_id": "SQL"},
                      {"notify_direct": 2}, {"recipient_exists": None}):
            with self.subTest(extra=extra), self.assertRaises(generator.PlanError):
                generator.build_plan([{**ROW, **extra}])
        with self.assertRaises(generator.PlanError):
            generator.build_plan([ROW, ROW])

    def test_plan_size_and_count_limits(self):
        with self.assertRaisesRegex(generator.PlanError, "plan_count_exceeded"):
            generator.build_plan([{**ROW, "notification_id": i + 1} for i in range(1001)])
        with self.assertRaisesRegex(generator.PlanError, "PLAN_INPUT_TOO_LARGE"):
            generator.build_plan([{**ROW, "notification_id": i + 1} for i in range(600)])

    def test_cli_private_exclusive_output_aggregate_only_and_no_network(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "audit.json"
            output = Path(directory) / "plan.json"
            audit.write_text(json.dumps({"notifications": [ROW]}))
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("sys.argv", ["plan", "--audit-json", str(audit), "--output", str(output)]), \
                 patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(generator.main(), 0)
                first = output.read_bytes()
                self.assertEqual(generator.main(), 1)  # never overwrite an existing exact plan
                self.assertEqual(output.read_bytes(), first)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            for private in ("70190", "1743", "PRIVATE_REASON", "987654321", "e" * 64):
                self.assertNotIn(private, stdout.getvalue() + stderr.getvalue())


class PendingWorkflowTests(unittest.TestCase):
    def test_each_action_has_fixed_endpoint_agent_confirmation_and_exact_bytes(self):
        for action in ("preview", "apply"):
            with self.subTest(action=action):
                opener = Opener(Response(200, SUMMARY))
                result = transport.run_request(action, "true", PLAN, "https://worker.example", TOKEN,
                                               operation="pending_reconciliation", opener=opener)
                request, timeout = opener.calls[0]
                self.assertTrue(result.ok)
                self.assertEqual(request.full_url, "https://worker.example/internal/notifications/reconcile/" + action)
                self.assertEqual(request.get_header("User-agent"), "DenmarkTgBot-PendingReconciliation-Maintenance/1.0")
                self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
                self.assertTrue(request.data.startswith(b'{"plan":' + PLAN.encode()))
                body = json.loads(request.data)
                self.assertEqual(body["plan"], json.loads(PLAN))
                self.assertEqual(timeout, 20)
                if action == "apply":
                    self.assertEqual(body["confirmation"], "APPLY_PINNED_PENDING_RECONCILIATION")
                else:
                    self.assertNotIn("confirmation", body)

    def test_unconfirmed_apply_has_no_http_and_no_temp_data(self):
        opener = Opener(Response(200, SUMMARY))
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(transport.WorkflowError):
            transport.run_request("apply", "false", PLAN, "https://worker.example", TOKEN,
                                  operation="pending_reconciliation", opener=opener, temp_directory=directory)
        self.assertEqual(opener.calls, [])

    def test_event_file_cli_filters_private_response_and_plan_from_all_output(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            summary = Path(directory) / "summary.md"
            event.write_text(json.dumps({"inputs": {"plan_json": PLAN}}))
            output, errors = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"INPUT_ACTION": "preview", "INPUT_CONFIRM": "false", "GITHUB_EVENT_PATH": str(event),
                 "INGEST_API_URL": "https://worker.example", "INGEST_API_TOKEN": TOKEN, "GITHUB_STEP_SUMMARY": str(summary)}), \
                 patch.object(transport.urllib.request, "build_opener", return_value=Opener(Response(200, SUMMARY))), \
                 redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(transport.main(operation="pending_reconciliation"), 0)
            rendered = output.getvalue() + errors.getvalue() + summary.read_text()
            for private in (PLAN, "70190", "1743", "PRIVATE_REASON", "987654321", TOKEN, "e" * 64, "reclassified_irrelevant"):
                self.assertNotIn(private, rendered)
            self.assertIn("ready=1", rendered)
            self.assertIn("already_reconciled", rendered)
            self.assertIn("a" * 64, rendered)  # only the server plan hash is intentionally public

    def test_http_non_success_fails_once_filters_response_and_cleans_temp(self):
        error = urllib.error.HTTPError("https://worker.example", 409, "PRIVATE_REASON", {}, io.BytesIO(json.dumps(SUMMARY).encode()))
        opener = Opener(error=error)
        with tempfile.TemporaryDirectory() as directory:
            result = transport.run_request("apply", "true", PLAN, "https://worker.example", TOKEN,
                                           operation="pending_reconciliation", opener=opener, temp_directory=directory)
            self.assertEqual(os.listdir(directory), [])
        self.assertFalse(result.ok)
        self.assertEqual(result.category, "worker_http_409")
        self.assertEqual(len(opener.calls), 1)
        self.assertNotIn(TOKEN, json.dumps(result.summary))
        self.assertNotIn("entries", result.summary)

    def test_workflow_dispatch_only_private_event_transport_and_shared_serialization(self):
        text = Path(".github/workflows/pending-reconciliation-maintenance.yml").read_text()
        self.assertIn("name: Controlled pending reconciliation", text)
        self.assertIn("on:\n  workflow_dispatch:", text)
        self.assertIn("permissions:\n  contents: read", text)
        self.assertIn("group: production-reclassification", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("default: false", text)
        self.assertIn("GITHUB_EVENT_PATH: ${{ github.event_path }}", text)
        self.assertIn("INGEST_API_URL: ${{ vars.INGEST_API_URL }}", text)
        self.assertIn("INGEST_API_TOKEN: ${{ secrets.INGEST_API_TOKEN }}", text)
        self.assertIn("python3 -m tools.pending_reconciliation_workflow", text)
        self.assertNotIn("inputs.plan_json", text)
        self.assertNotIn("INPUT_PLAN_JSON", text)
        for forbidden in ("schedule:", "push:", "workflow_run:", "pull_request:", "set -x", "printenv", "echo $", "curl", "wrangler"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
