from __future__ import annotations

import unittest
from pathlib import Path

from collectors.workflow_support import WorkflowGateError, render_summary, validate_execution_mode


class WorkflowSupportTests(unittest.TestCase):
    def test_manual_mode_requires_explicit_confirmation(self):
        self.assertEqual(validate_execution_mode("workflow_dispatch", "true", ""), "manual")
        for value in ("", "false", "yes"):
            with self.subTest(value=value), self.assertRaises(WorkflowGateError):
                validate_execution_mode("workflow_dispatch", value, "true")

    def test_scheduled_mode_requires_exact_enabled_gate(self):
        self.assertEqual(validate_execution_mode("schedule", "false", "true"), "automated")
        for value in ("", "false", "TRUE", "yes"):
            with self.subTest(value=value), self.assertRaises(WorkflowGateError):
                validate_execution_mode("schedule", "true", value)

    def test_unknown_event_fails_closed(self):
        with self.assertRaises(WorkflowGateError):
            validate_execution_mode("repository_dispatch", "true", "true")

    def test_summary_includes_feed_counts_and_sanitized_failure_category(self):
        collection = {
            "feeds": {
                "immigration_ministry": {"status": "success", "entries_parsed": 3},
                "immigration_category": {"status": "unavailable", "entries_parsed": 0},
            },
            "publications": [{}, {}, {}],
        }
        rendered = render_summary(collection, None, run_id="123", gate_status="success", collect_status="failure", ingest_status="skipped")
        self.assertIn("Unique publications: **3**", rendered)
        self.assertIn("`immigration_ministry`: `success` (3 entries)", rendered)
        self.assertIn("collection_failed_or_incomplete", rendered)
        self.assertNotIn("token", rendered.lower())

    def test_summary_reports_confirmed_commit_status(self):
        rendered = render_summary(None, {"status": "COMMITTED", "run_id": "safe-id"}, run_id="123", gate_status="success", collect_status="success", ingest_status="success")
        self.assertIn("Ingestion: `COMMITTED`", rendered)

    def test_summary_reports_closed_safety_gate(self):
        rendered = render_summary(None, None, run_id="123", gate_status="failure", collect_status="not_run", ingest_status="not_run")
        self.assertIn("safety_gate_rejected", rendered)

    def test_workflow_has_no_active_schedule_and_retains_lock_and_time_limit(self):
        workflow = Path(".github/workflows/hoeringsportalen-incremental.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("  schedule:", workflow)
        self.assertIn("group: hoeringsportalen-incremental", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("timeout-minutes: 10", workflow)
        self.assertIn("HOERINGSPORTALEN_AUTOMATION_ENABLED: ${{ vars.HOERINGSPORTALEN_AUTOMATION_ENABLED }}", workflow)


if __name__ == "__main__":
    unittest.main()
