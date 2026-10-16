from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from collectors.workflow_support import WorkflowGateError, render_summary, validate_execution_mode


ROOT = Path(__file__).resolve().parents[1]


class WorkflowSupportTests(unittest.TestCase):
    def run_gate_cli(self, event: str, *, mode: str | None = "dry_run", confirmation: str | None = None, automation: str | None = None):
        env = os.environ.copy()
        for name in ("REQUESTED_MODE", "CONFIRM_PRODUCTION_INGESTION", "HOERINGSPORTALEN_AUTOMATION_ENABLED"):
            env.pop(name, None)
        env["GITHUB_EVENT_NAME"] = event
        if mode is not None:
            env["REQUESTED_MODE"] = mode
        if confirmation is not None:
            env["CONFIRM_PRODUCTION_INGESTION"] = confirmation
        if automation is not None:
            env["HOERINGSPORTALEN_AUTOMATION_ENABLED"] = automation
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "github-output"
            env["GITHUB_OUTPUT"] = str(output_path)
            result = subprocess.run(
                [sys.executable, "-m", "collectors.workflow_support", "gate"],
                cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
            output = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        return result, output

    def test_direct_mode_validation(self):
        self.assertEqual(validate_execution_mode("workflow_dispatch", "dry_run", "false", ""), "dry_run")
        self.assertEqual(validate_execution_mode("workflow_dispatch", "production_ingest", "true", ""), "production_ingest")
        self.assertEqual(validate_execution_mode("schedule", "", "false", "true"), "production_ingest")
        for args in (
            ("workflow_dispatch", "unknown", "true", "true"),
            ("workflow_dispatch", "production_ingest", "false", ""),
            ("schedule", "", "", "TRUE"),
            ("repository_dispatch", "dry_run", "true", "true"),
        ):
            with self.subTest(args=args), self.assertRaises(WorkflowGateError):
                validate_execution_mode(*args)

    def test_actual_cli_modes_and_fail_closed_scenarios(self):
        cases = (
            ("workflow_dispatch", "dry_run", None, None, 0, "dry_run"),
            ("workflow_dispatch", "production_ingest", "true", None, 0, "production_ingest"),
            ("workflow_dispatch", "production_ingest", None, None, 2, None),
            ("workflow_dispatch", "production_ingest", "TRUE", None, 2, None),
            ("workflow_dispatch", "invalid", "true", None, 2, None),
            ("schedule", None, None, "true", 0, "production_ingest"),
            ("schedule", None, None, None, 2, None),
            ("schedule", None, None, "false", 2, None),
            ("schedule", None, None, "TRUE", 2, None),
        )
        for event, mode, confirmation, automation, expected_code, expected_mode in cases:
            with self.subTest(event=event, mode=mode, confirmation=confirmation, automation=automation):
                result, output = self.run_gate_cli(event, mode=mode, confirmation=confirmation, automation=automation)
                self.assertEqual(result.returncode, expected_code, result.stderr)
                if expected_code == 0:
                    self.assertRegex(output, r"run_id=[0-9a-f-]{36}\n")
                    self.assertIn(f"mode={expected_mode}\n", output)
                else:
                    self.assertEqual(output, "")

    def test_gate_order_and_dry_run_have_no_production_endpoint_credentials(self):
        workflow = (ROOT / ".github/workflows/hoeringsportalen-incremental.yml").read_text(encoding="utf-8")
        support = (ROOT / "collectors/workflow_support.py").read_text(encoding="utf-8")
        self.assertIn("HOERINGSPORTALEN_AUTOMATION_ENABLED: ${{ vars.HOERINGSPORTALEN_AUTOMATION_ENABLED }}", workflow)
        self.assertIn('os.environ.get("HOERINGSPORTALEN_AUTOMATION_ENABLED", "")', support)
        gate = workflow.index("python -m collectors.workflow_support gate")
        collect = workflow.index("python -m collectors.main")
        dry_run = workflow.index("--dry-run")
        production = workflow.index("python -m collectors.ingestion_client", dry_run + 1)
        self.assertLess(gate, collect)
        self.assertLess(collect, dry_run)
        self.assertLess(dry_run, production)
        dry_step = workflow.split("- name: Validate collected records without production writes", 1)[1].split("- name: Submit complete incremental run to production", 1)[0]
        self.assertNotIn("INGEST_API_TOKEN", dry_step)
        self.assertNotIn("INGEST_API_URL", dry_step)
        self.assertIn("--dry-run", dry_step)
        self.assertIn("steps.collect.outcome == 'success'", workflow)

    def test_dry_run_summary_is_explicit_about_status_completeness_and_no_write(self):
        collection = {
            "feeds": {
                "immigration_ministry": {"status": "success", "entries_parsed": 1, "invalid_entries": 0, "invalid_timestamps": 0, "missing_updated_at": 0, "invalid_cursor_ids": 0},
                "immigration_category": {"status": "success_empty", "entries_parsed": 0, "invalid_entries": 0, "invalid_timestamps": 0, "missing_updated_at": 0, "invalid_cursor_ids": 0},
            },
            "publications": [],
        }
        rendered = render_summary(collection, {"dry_run": True, "would_submit": True, "relevance_counts": {"DIRECT": 0, "POTENTIAL": 0, "IRRELEVANT": 0}}, run_id="123", mode="dry_run", gate_status="success", collect_status="success", ingest_status="success")
        self.assertIn("Collection complete: `true`", rendered)
        self.assertIn("Ingestion: `DRY_RUN`", rendered)
        self.assertIn("Production write: **No**", rendered)

    def test_unknown_mode_rejected_before_collection_in_workflow(self):
        result, output = self.run_gate_cli("workflow_dispatch", mode="typo", confirmation="true")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(output, "")

    def test_workflow_defaults_to_dry_run_and_has_exact_gated_schedule(self):
        workflow = (ROOT / ".github/workflows/hoeringsportalen-incremental.yml").read_text(encoding="utf-8")
        self.assertIn("default: dry_run", workflow)
        self.assertIn("- production_ingest", workflow)
        self.assertIn("  schedule:\n    - cron: '17 6,14,22 * * *'", workflow)
        self.assertIn('if: ${{ github.event_name == \'schedule\' || github.event_name == \'workflow_dispatch\' }}', workflow)
        self.assertIn("HOERINGSPORTALEN_AUTOMATION_ENABLED: ${{ vars.HOERINGSPORTALEN_AUTOMATION_ENABLED }}", workflow)
        self.assertIn("group: hoeringsportalen-incremental", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("timeout-minutes: 10", workflow)


if __name__ == "__main__":
    unittest.main()
