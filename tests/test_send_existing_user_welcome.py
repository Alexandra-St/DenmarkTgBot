from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.send_existing_user_welcome import (
    MARKER,
    OWNER_TELEGRAM_ID,
    SafeUtilityError,
    attempt_once,
    create_private_attempt_marker,
    localized_welcome,
    rows_from_wrangler,
    select_beta_recipient,
    send_telegram_once,
)


class ExistingUserWelcomeTests(unittest.TestCase):
    def test_loads_exact_current_ukrainian_localization(self) -> None:
        text = localized_welcome("uk")
        self.assertIn("🇩🇰 Вітаємо в DenmarkTgBot!", text)
        self.assertIn("Høringsportalen", text)
        self.assertIn("Folketinget", text)
        self.assertLess(len(text), 4096)

    def test_selects_non_owner_even_when_owner_is_the_ukrainian_user(self) -> None:
        output = json.dumps([{"results": [
            {"telegram_id": OWNER_TELEGRAM_ID, "language": "uk"},
            {"telegram_id": 222222, "language": "ru"},
        ]}])
        rows = rows_from_wrangler(output)
        self.assertEqual(select_beta_recipient(rows), (222222, "ru"))

    def test_uses_other_users_actual_saved_language(self) -> None:
        recipient, language = select_beta_recipient([
            {"telegram_id": OWNER_TELEGRAM_ID, "language": "ru"},
            {"telegram_id": 333333, "language": "uk"},
        ])
        self.assertEqual((recipient, language), (333333, "uk"))
        self.assertIn("Вітаємо в DenmarkTgBot!", localized_welcome(language))
        self.assertIn("Добро пожаловать в DenmarkTgBot!", localized_welcome("ru"))

    def test_refuses_wrong_count_missing_owner_or_invalid_language(self) -> None:
        with self.assertRaises(SafeUtilityError):
            select_beta_recipient([{"telegram_id": OWNER_TELEGRAM_ID, "language": "ru"}])
        with self.assertRaises(SafeUtilityError):
            select_beta_recipient([
                {"telegram_id": 111111, "language": "uk"},
                {"telegram_id": 222222, "language": "ru"},
            ])
        with self.assertRaises(SafeUtilityError):
            select_beta_recipient([
                {"telegram_id": OWNER_TELEGRAM_ID, "language": "ru"},
                {"telegram_id": 222222, "language": "da"},
            ])

    def test_owner_is_hard_blocked_even_if_passed_directly(self) -> None:
        sender = Mock()
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "attempt.marker"
            with self.assertRaises(SafeUtilityError):
                attempt_once("test-token", OWNER_TELEGRAM_ID, "welcome", marker, sender)
            self.assertFalse(marker.exists())
        sender.assert_not_called()
        with patch("tools.send_existing_user_welcome.urllib.request.build_opener") as build_opener:
            with self.assertRaises(SafeUtilityError):
                send_telegram_once("test-token", OWNER_TELEGRAM_ID, "welcome")
            build_opener.assert_not_called()

    def test_marker_is_private_exclusive_and_created_before_sender(self) -> None:
        self.assertNotEqual(MARKER.name, "existing-user-welcome-attempt.marker")
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "private" / "attempt.marker"
            sender = Mock()

            def verify_marker_first(token: str, recipient: int, text: str) -> None:
                self.assertTrue(marker.exists())
                self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)

            sender.side_effect = verify_marker_first
            attempt_once("test-token", 222222, "welcome", marker, sender)
            sender.assert_called_once_with("test-token", 222222, "welcome")
            with self.assertRaises(SafeUtilityError):
                attempt_once("test-token", 222222, "welcome", marker, sender)
            sender.assert_called_once()

    def test_send_performs_one_request_and_timeout_is_not_retried(self) -> None:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true}'
        opener = Mock()
        opener.open.return_value = response
        with patch("tools.send_existing_user_welcome.urllib.request.build_opener", return_value=opener):
            send_telegram_once("test-token", 222222, "welcome")
        opener.open.assert_called_once()

        timeout_opener = Mock()
        timeout_opener.open.side_effect = TimeoutError("timeout")
        with patch("tools.send_existing_user_welcome.urllib.request.build_opener", return_value=timeout_opener):
            with self.assertRaisesRegex(SafeUtilityError, "ambiguous"):
                send_telegram_once("test-token", 222222, "welcome")
        timeout_opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
