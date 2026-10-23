from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.folketinget_snapshot_alert import AlertError, alert_message, read_event, send_telegram

REPOSITORY = "Alexandra-St/DenmarkTgBot"


def payload(event="schedule", conclusion="failure", name="Folketinget snapshot incremental"):
    return {"workflow_run": {"event": event, "conclusion": conclusion, "id": 12345,
            "head_branch": "main", "head_sha": "a" * 40, "name": name}}


class FolketingetSnapshotAlertTests(unittest.TestCase):
    def test_scheduled_success_does_not_alert(self):
        self.assertIsNone(alert_message(payload(conclusion="success"), REPOSITORY))

    def test_scheduled_failure_alerts_with_required_metadata(self):
        message = alert_message(payload(), REPOSITORY)
        self.assertIn("⚠️ DenmarkTgBot: Folketinget scheduled run failed", message)
        self.assertIn("Conclusion: failure", message)
        self.assertIn("GitHub run ID: 12345", message)
        self.assertIn("Branch: main", message)
        self.assertIn("Commit SHA: " + "a" * 40, message)
        self.assertIn("https://github.com/Alexandra-St/DenmarkTgBot/actions/runs/12345", message)

    def test_scheduled_cancelled_and_skipped_alert(self):
        for conclusion in ("cancelled", "skipped"):
            with self.subTest(conclusion=conclusion):
                self.assertIsNotNone(alert_message(payload(conclusion=conclusion), REPOSITORY))

    def test_scheduled_other_abnormal_terminal_conclusion_alerts(self):
        self.assertIsNotNone(alert_message(payload(conclusion="timed_out"), REPOSITORY))
        self.assertIsNotNone(alert_message(payload(conclusion="action_required"), REPOSITORY))

    def test_manual_failure_does_not_alert(self):
        self.assertIsNone(alert_message(payload(event="workflow_dispatch"), REPOSITORY))

    def test_unrelated_workflow_does_not_alert(self):
        self.assertIsNone(alert_message(payload(name="Other workflow"), REPOSITORY))

    def test_invalid_terminal_data_fails_closed(self):
        data = payload()
        data["workflow_run"]["head_branch"] = "main\nforged"
        with self.assertRaises(AlertError):
            alert_message(data, REPOSITORY)

    def test_telegram_failure_does_not_expose_response_body(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, _): return b'{"ok":false,"description":"private response"}'
        with patch("tools.folketinget_snapshot_alert.urllib.request.urlopen", return_value=Response()):
            with self.assertRaisesRegex(AlertError, "telegram_response_invalid"):
                send_telegram("safe", "token", "123")

    def test_workflow_is_scoped_to_scheduled_target_and_configured_secrets(self):
        workflow = Path(".github/workflows/folketinget-snapshot-alert.yml").read_text(encoding="utf-8")
        self.assertIn('workflows: ["Folketinget snapshot incremental"]', workflow)
        self.assertIn("types: [completed]", workflow)
        self.assertIn("github.event.workflow_run.event == 'schedule'", workflow)
        self.assertIn("github.event.workflow_run.conclusion != 'success'", workflow)
        self.assertIn("secrets.TELEGRAM_BOT_TOKEN", workflow)
        self.assertIn("secrets.TELEGRAM_OPERATIONAL_ALERT_CHAT_ID", workflow)
        secret_refs = re.findall(r"secrets\.([A-Z0-9_]+)", workflow)
        self.assertEqual(set(secret_refs), {"TELEGRAM_BOT_TOKEN", "TELEGRAM_OPERATIONAL_ALERT_CHAT_ID"})
        self.assertNotIn("curl", workflow)
        self.assertNotIn("wrangler", workflow)

    def test_event_file_is_bounded_and_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            event_path.write_text(json.dumps(payload()), encoding="utf-8")
            self.assertEqual(read_event(str(event_path))["workflow_run"]["event"], "schedule")


if __name__ == "__main__":
    unittest.main()
