"""Send a bounded Telegram alert for a scheduled Folketinget workflow failure."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

EXPECTED_WORKFLOW_NAME = "Folketinget snapshot incremental"
SAFE_CONCLUSION = re.compile(r"[a-z_]{1,32}\Z")
SAFE_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")


class AlertError(Exception):
    pass


def alert_message(event: dict[str, Any], repository: str) -> str | None:
    """Return safe message for a scheduled failed run, else None."""
    run = event.get("workflow_run")
    if not isinstance(run, dict):
        return None
    if run.get("name") != EXPECTED_WORKFLOW_NAME:
        return None
    if run.get("event") != "schedule":
        return None
    conclusion = run.get("conclusion")
    if not isinstance(conclusion, str) or not SAFE_CONCLUSION.fullmatch(conclusion):
        raise AlertError("workflow_conclusion_invalid")
    if conclusion == "success":
        return None
    run_id = run.get("id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise AlertError("workflow_run_id_invalid")
    branch = run.get("head_branch")
    sha = run.get("head_sha")
    if (not isinstance(branch, str) or not branch or len(branch) > 255
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in branch)):
        raise AlertError("workflow_branch_invalid")
    if not isinstance(sha, str) or not SAFE_SHA.fullmatch(sha):
        raise AlertError("workflow_commit_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise AlertError("repository_invalid")
    url = f"https://github.com/{repository}/actions/runs/{run_id}"
    return (
        "⚠️ DenmarkTgBot: Folketinget scheduled run failed\n"
        f"Conclusion: {conclusion}\n"
        f"GitHub run ID: {run_id}\n"
        f"Branch: {branch}\n"
        f"Commit SHA: {sha}\n"
        f"Actions run: {url}"
    )


def diagnostic_message(event_name: str, confirmation: str) -> str | None:
    """Return only the fixed diagnostic text after an explicit manual confirmation."""
    if event_name != "workflow_dispatch" or confirmation != "true":
        return None
    return "🧪 DenmarkTgBot — operational alert test"


def read_event(path: str) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 1_000_000:
            raise AlertError("workflow_event_too_large")
        value = json.loads(raw)
    except AlertError:
        raise
    except Exception as exc:
        raise AlertError("workflow_event_invalid") from exc
    if not isinstance(value, dict):
        raise AlertError("workflow_event_invalid")
    return value


def send_telegram(message: str, token: str, chat_id: str) -> None:
    if not token or not chat_id:
        raise AlertError("telegram_configuration_missing")
    if len(message.encode("utf-8")) > 4096:
        raise AlertError("telegram_message_too_large")
    payload = json.dumps({"chat_id": chat_id, "text": message}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "DenmarkTgBot-SnapshotAlert/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(16_385)
            if len(raw) > 16_384 or response.status < 200 or response.status >= 300:
                raise AlertError("telegram_request_failed")
    except AlertError:
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code
        if status in (400, 401, 403, 429):
            category = f"telegram_http_{status}"
        elif isinstance(status, int) and 500 <= status <= 599:
            category = "telegram_http_5xx"
        else:
            category = "telegram_http_error"
        raise AlertError(category) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AlertError("telegram_transport_failed") from None
    try:
        result = json.loads(raw)
    except Exception as exc:
        raise AlertError("telegram_response_invalid") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise AlertError("telegram_response_invalid")


def main() -> int:
    try:
        event_name = os.environ.get("GITHUB_EVENT_NAME", "")
        if event_name == "workflow_dispatch":
            message = diagnostic_message(event_name, os.environ.get("INPUT_CONFIRM_TEST_ALERT", ""))
            if message is None:
                print("diagnostic_alert=NOT_CONFIRMED")
                return 0
        elif event_name == "workflow_run":
            event = read_event(os.environ.get("GITHUB_EVENT_PATH", ""))
            message = alert_message(event, os.environ.get("GITHUB_REPOSITORY", ""))
            if message is None:
                print("scheduled_alert=NOT_REQUIRED")
                return 0
        else:
            print("alert=NOT_APPLICABLE")
            return 0
        send_telegram(
            message,
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_OPERATIONAL_ALERT_CHAT_ID", ""),
        )
        print("alert=SENT")
        return 0
    except AlertError as exc:
        print(f"alert=FAIL category={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
