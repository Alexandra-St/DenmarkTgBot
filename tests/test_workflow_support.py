from __future__ import annotations

import unittest
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from collectors.workflow_support import WorkflowGateError, render_summary, validate_execution_mode


class WorkflowSupportTests(unittest.TestCase):
    def run_gate_cli(self, event: str, *, confirmation: str | None = None, automation: str | None = None):
        env = os.environ.copy()
        env["GITHUB_EVENT_NAME"] = event
        env.pop("CONFIRM_PRODUCTION_INGESTION", None)
        env.pop("HOERINGSPORTALEN_AUTOMATION_ENABLED", None)
        if confirmation is not None:
            env["CONFIRM_PRODUCTION_INGESTION"] = confirmation
        if automation is not None:
            env["HOERINGSPORTALEN_AUTOMATION_ENABLED"] = automation
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "github-output"
            env["GITHUB_OUTPUT"] = str(output_path)
            result = subprocess.run(
                [sys.executable, "-m", "collectors.workflow_support", "gate"],
                cwd=Path(__file__).resolve().parents[1], env=env,
                text=True, capture_output=True, check=False,
            )
            output = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        return result, output

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

    def test_actual_cli_gate_scenarios_and_rejection_has_no_run_output(self):
        cases = (
            ("schedule", None, "true", 0),
            ("schedule", None, None, 2),
            ("schedule", None, "false", 2),
            ("schedule", None, "TRUE", 2),
            ("workflow_dispatch", "true", None, 0),
            ("workflow_dispatch", None, None, 2),
        )
        for event, confirmation, automation, expected_code in cases:
            with self.subTest(event=event, confirmation=confirmation, automation=automation):
                result, output = self.run_gate_cli(event, confirmation=confirmation, automation=automation)
                self.assertEqual(result.returncode, expected_code, result.stderr)
                if expected_code == 0:
                    self.assertRegex(output, r"run_id=[0-9a-f-]{36}\n")
                else:
                    self.assertEqual(output, "")

    def test_cli_uses_exact_workflow_environment_name_and_gate_precedes_side_effect_steps(self):
        workflow = Path(".github/workflows/hoeringsportalen-incremental.yml").read_text(encoding="utf-8")
        support = Path("collectors/workflow_support.py").read_text(encoding="utf-8")
        self.assertIn("HOERINGSPORTALEN_AUTOMATION_ENABLED: ${{ vars.HOERINGSPORTALEN_AUTOMATION_ENABLED }}", workflow)
        self.assertIn('os.environ.get("HOERINGSPORTALEN_AUTOMATION_ENABLED", "")', support)
        self.assertLess(workflow.index("python -m collectors.workflow_support gate"), workflow.index("python -m collectors.main"))
        self.assertLess(workflow.index("python -m collectors.main"), workflow.index("python -m collectors.ingestion_client"))
        self.assertIn("if: ${{ steps.collect.outcome == 'success' }}", workflow)

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
