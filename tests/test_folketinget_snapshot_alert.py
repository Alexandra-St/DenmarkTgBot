from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from tools.folketinget_snapshot_alert import (
    AlertError, alert_message, diagnostic_message, main, read_event, send_telegram,
)

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

    def test_manual_diagnostic_requires_exact_confirmation_and_fixed_text(self):
        expected = "🧪 DenmarkTgBot — operational alert test"
        self.assertEqual(diagnostic_message("workflow_dispatch", "true"), expected)
        self.assertIsNone(diagnostic_message("workflow_dispatch", "false"))
        self.assertIsNone(diagnostic_message("workflow_dispatch", "TRUE"))
        self.assertIsNone(diagnostic_message("workflow_dispatch", ""))
        self.assertIsNone(diagnostic_message("workflow_run", "true"))

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

    def test_telegram_http_400_and_401_are_safe_categories(self):
        for status in (400, 401):
            body = b'{"ok":false,"description":"private telegram description"}'
            failure = urllib.error.HTTPError(
                "https://api.telegram.org/botsecret-token/sendMessage",
                status, "private response text", {}, BytesIO(body),
            )
            with self.subTest(status=status), patch(
                "tools.folketinget_snapshot_alert.urllib.request.urlopen", side_effect=failure
            ):
                with self.assertRaisesRegex(AlertError, f"telegram_http_{status}") as raised:
                    send_telegram("safe", "secret-token", "private-chat")
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn("secret-token", str(raised.exception))

    def test_telegram_http_5xx_is_bounded_category(self):
        failure = urllib.error.HTTPError("https://api.telegram.org/botsecret/sendMessage", 503,
                                         "private description", {}, BytesIO(b"secret body"))
        with patch("tools.folketinget_snapshot_alert.urllib.request.urlopen", side_effect=failure):
            with self.assertRaisesRegex(AlertError, "telegram_http_5xx"):
                send_telegram("safe", "secret", "chat")

    def test_telegram_transport_error_has_safe_category(self):
        with patch("tools.folketinget_snapshot_alert.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("private network detail")):
            with self.assertRaisesRegex(AlertError, "telegram_transport_failed"):
                send_telegram("safe", "token", "chat")

    def test_workflow_is_scoped_to_scheduled_target_and_configured_secrets(self):
        workflow = Path(".github/workflows/folketinget-snapshot-alert.yml").read_text(encoding="utf-8")
        self.assertIn('workflows: ["Folketinget snapshot incremental"]', workflow)
        self.assertIn("types: [completed]", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("default: false", workflow)
        self.assertIn("inputs.confirm_test_alert == true", workflow)
        self.assertIn("github.event_name == 'workflow_dispatch'", workflow)
        self.assertIn("github.event.workflow_run.event == 'schedule'", workflow)
        self.assertIn("github.event.workflow_run.conclusion != 'success'", workflow)
        self.assertIn("secrets.TELEGRAM_BOT_TOKEN", workflow)
        self.assertIn("secrets.TELEGRAM_OPERATIONAL_ALERT_CHAT_ID", workflow)
        secret_refs = re.findall(r"secrets\.([A-Z0-9_]+)", workflow)
        self.assertEqual(set(secret_refs), {"TELEGRAM_BOT_TOKEN", "TELEGRAM_OPERATIONAL_ALERT_CHAT_ID"})
        self.assertNotIn("curl", workflow)
        self.assertNotIn("wrangler", workflow)

    def test_unconfirmed_manual_diagnostic_never_calls_telegram(self):
        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "INPUT_CONFIRM_TEST_ALERT": "false",
        }, clear=False), patch("tools.folketinget_snapshot_alert.send_telegram") as send:
            self.assertEqual(main(), 0)
        send.assert_not_called()

    def test_manual_send_failure_does_not_log_secrets(self):
        token = "secret-bot-token-fixture"
        chat_id = "private-chat-fixture"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "INPUT_CONFIRM_TEST_ALERT": "true",
            "TELEGRAM_BOT_TOKEN": token,
            "TELEGRAM_OPERATIONAL_ALERT_CHAT_ID": chat_id,
        }, clear=False), patch("tools.folketinget_snapshot_alert.urllib.request.urlopen",
                               side_effect=urllib.error.URLError("private transport details")), \
             patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            self.assertEqual(main(), 1)
        self.assertIn("category=telegram_transport_failed", stderr.getvalue())
        self.assertNotIn(token, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(chat_id, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("private transport details", stdout.getvalue() + stderr.getvalue())

    def test_http_error_logs_only_status_category(self):
        token = "secret-bot-token-fixture"
        chat_id = "private-chat-fixture"
        body = b'{"ok":false,"description":"private telegram description"}'
        failure = urllib.error.HTTPError(
            f"https://api.telegram.org/bot{token}/sendMessage",
            401, "private exception text", {}, BytesIO(body),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "INPUT_CONFIRM_TEST_ALERT": "true",
            "TELEGRAM_BOT_TOKEN": token,
            "TELEGRAM_OPERATIONAL_ALERT_CHAT_ID": chat_id,
        }, clear=False), patch("tools.folketinget_snapshot_alert.urllib.request.urlopen",
                               side_effect=failure), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            self.assertEqual(main(), 1)
        output = stdout.getvalue() + stderr.getvalue()
        self.assertIn("category=telegram_http_401", output)
        for private_value in (token, chat_id, "private telegram description", "private exception text"):
            self.assertNotIn(private_value, output)

    def test_event_file_is_bounded_and_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            event_path.write_text(json.dumps(payload()), encoding="utf-8")
            self.assertEqual(read_event(str(event_path))["workflow_run"]["event"], "schedule")


if __name__ == "__main__":
    unittest.main()
