#!/usr/bin/env python3
"""Send one existing Ukrainian beta tester the current localized welcome."""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DATABASE = "denmarktgbot-db"
SQL = "SELECT telegram_id, language FROM users;"
MARKER = Path.home() / ".local" / "state" / "denmarktgbot" / "existing-user-welcome-attempt.marker"
MESSAGE_PATTERN = re.compile(r'^\s*firstTimeWelcome:\s*("(?:\\.|[^"\\])*")\s*,?\s*$', re.MULTILINE)


class SafeUtilityError(Exception):
    """An error whose message is safe to show in the terminal."""


def localized_welcome(source: Path | None = None) -> str:
    source = source or ROOT / "worker" / "src" / "localization" / "uk.ts"
    match = MESSAGE_PATTERN.search(source.read_text(encoding="utf-8"))
    if not match:
        raise SafeUtilityError("Could not read firstTimeWelcome from the Ukrainian localization source.")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SafeUtilityError("The Ukrainian welcome string is not a valid quoted string.") from exc


def _json_payload(output: str) -> object:
    """Parse Wrangler's --json output without printing or echoing it."""
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        # Wrangler releases may emit a short status preamble before JSON.
        for index, character in enumerate(output):
            if character not in "[{":
                continue
            try:
                value, _ = json.JSONDecoder().raw_decode(output[index:])
                return value
            except json.JSONDecodeError:
                continue
    raise SafeUtilityError("Wrangler did not return parseable JSON.")


def rows_from_wrangler(output: str) -> list[dict[str, object]]:
    payload = _json_payload(output)
    if isinstance(payload, dict):
        payload = payload.get("results", payload.get("result", payload))
    if isinstance(payload, list):
        rows: list[dict[str, object]] = []
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get("results"), list):
                rows.extend(row for row in item["results"] if isinstance(row, dict))
            elif isinstance(item, dict) and "telegram_id" in item:
                rows.append(item)
        return rows
    raise SafeUtilityError("Wrangler returned an unexpected result format.")


def select_ukrainian_recipient(rows: Sequence[dict[str, object]]) -> int:
    if len(rows) != 2:
        raise SafeUtilityError("Expected exactly two registered users; no message was sent.")
    languages = [row.get("language") for row in rows]
    if any(language not in ("ru", "uk") for language in languages) or sorted(languages) != ["ru", "uk"]:
        raise SafeUtilityError("Expected one Russian and one Ukrainian user; no message was sent.")
    matches = [row for row in rows if row.get("language") == "uk"]
    if len(matches) != 1:
        raise SafeUtilityError("Expected exactly one Ukrainian-language recipient; no message was sent.")
    recipient = matches[0].get("telegram_id")
    if isinstance(recipient, bool) or not isinstance(recipient, int) or recipient <= 0:
        raise SafeUtilityError("The matching account has an invalid identifier; no message was sent.")
    return recipient


def read_production_users() -> list[dict[str, object]]:
    command = [
        "npx", "wrangler", "d1", "execute", DATABASE, "--remote", "--json",
        "--command", SQL,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT / "worker",
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SafeUtilityError("Read-only production user query timed out; no message was sent.") from exc
    except OSError as exc:
        raise SafeUtilityError("Could not start Wrangler; no message was sent.") from exc
    if result.returncode != 0:
        # Wrangler output is deliberately suppressed: never echo account data.
        raise SafeUtilityError("Read-only production user query failed; no message was sent.")
    return rows_from_wrangler(result.stdout)


def create_private_attempt_marker(path: Path = MARKER) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise SafeUtilityError("A welcome-send attempt marker already exists; refusing to send again.") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as marker:
            marker.write(f"attempted_at={datetime.now(timezone.utc).isoformat()}\n")
            marker.flush()
            os.fsync(marker.fileno())
    except Exception:
        # Retain the marker if anything fails after exclusive creation.
        raise SafeUtilityError("Could not safely finalize the one-time attempt marker; no message was sent.")


def send_telegram_once(token: str, recipient: int, text: str, *, timeout: float = 15) -> None:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": recipient, "text": text}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirectHandler)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SafeUtilityError(f"Telegram returned HTTP {exc.code}; the one-time attempt is recorded and will not be retried.") from None
    except (TimeoutError, socket.timeout, urllib.error.URLError):
        raise SafeUtilityError("Delivery outcome is ambiguous due to a network timeout/error. Do not retry; the one-time attempt is recorded.") from None
    except (OSError, json.JSONDecodeError):
        raise SafeUtilityError("Delivery outcome could not be confirmed. Do not retry; the one-time attempt is recorded.") from None
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise SafeUtilityError("Telegram did not confirm delivery. The one-time attempt is recorded; do not retry.")


def attempt_once(
    token: str,
    recipient: int,
    text: str,
    marker_path: Path,
    sender: Callable[[str, int, str], None] = send_telegram_once,
) -> None:
    create_private_attempt_marker(marker_path)
    sender(token, recipient, text)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def main() -> int:
    try:
        if MARKER.exists():
            raise SafeUtilityError("A welcome-send attempt marker already exists; refusing to send again.")
        recipient = select_ukrainian_recipient(read_production_users())
        text = localized_welcome()
        print("Verified exactly one Ukrainian-language account among the two registered users. Account ID is hidden.")
        confirmation = input("Send the Ukrainian welcome once to that account? Type SEND to continue: ").strip()
        if confirmation != "SEND":
            print("Cancelled. No message was sent.")
            return 0
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            token = getpass.getpass("Telegram bot token (input hidden): ").strip()
        if not token:
            raise SafeUtilityError("No Telegram token provided; no message was sent.")
        attempt_once(token, recipient, text, MARKER)
        print("Telegram confirmed the one-time welcome message.")
        return 0
    except SafeUtilityError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
