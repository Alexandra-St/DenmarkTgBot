from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.send_existing_user_welcome import (
    SafeUtilityError,
    attempt_once,
    create_private_attempt_marker,
    localized_welcome,
    rows_from_wrangler,
    select_ukrainian_recipient,
    send_telegram_once,
)


class ExistingUserWelcomeTests(unittest.TestCase):
    def test_loads_exact_current_ukrainian_localization(self) -> None:
        text = localized_welcome()
        self.assertIn("🇩🇰 Вітаємо в DenmarkBot!", text)
        self.assertIn("Høringsportalen", text)
        self.assertIn("Folketinget", text)
        self.assertLess(len(text), 4096)

    def test_parses_wrangler_json_and_selects_only_ukrainian_row(self) -> None:
        output = json.dumps([{"results": [
            {"telegram_id": 111111, "language": "ru"},
            {"telegram_id": 222222, "language": "uk"},
        ]}])
        rows = rows_from_wrangler(output)
        self.assertEqual(select_ukrainian_recipient(rows), 222222)

    def test_refuses_unexpected_user_count_or_language_distribution(self) -> None:
        with self.assertRaises(SafeUtilityError):
            select_ukrainian_recipient([{"telegram_id": 1, "language": "uk"}])
        with self.assertRaises(SafeUtilityError):
            select_ukrainian_recipient([
                {"telegram_id": 1, "language": "uk"},
                {"telegram_id": 2, "language": "uk"},
            ])

    def test_marker_is_private_exclusive_and_created_before_sender(self) -> None:
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
