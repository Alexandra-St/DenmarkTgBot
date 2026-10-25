"""Safe transport for the manual controlled-reclassification GitHub workflow."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

MAX_PLAN_BYTES = 61_440
MAX_RESPONSE_BYTES = 256 * 1024
ALLOWED_COUNTS = ("expected_count", "total_count", "ready", "already_applied", "stale", "missing", "invalid", "blocked")
ALLOWED_STATUSES = {"PREVIEW", "APPLIED", "PARTIAL", "CONFLICT", "INDETERMINATE", "BLOCKED"}
CONFIRMATION = "APPLY_PINNED_RECLASSIFICATION_PLAN"
ENDPOINTS = {
    "preview": "/internal/reclassification/preview",
    "apply": "/internal/reclassification/apply",
}


class WorkflowError(Exception):
    def __init__(self, category: str, *, size: int | None = None) -> None:
        super().__init__(category)
        self.category = category
        self.size = size


@dataclass(frozen=True)
class Result:
    ok: bool
    category: str | None
    summary: dict[str, Any]


def _safe_summary(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    status = value.get("status")
    if isinstance(status, str) and status in ALLOWED_STATUSES:
        result["status"] = status
    digest = value.get("plan_sha256")
    if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest):
        result["plan_sha256"] = digest
    for key in ALLOWED_COUNTS:
        count = value.get(key)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            result[key] = count
    return result or None


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def _reject_json_constant(_: str) -> None:
    raise ValueError("invalid_json_constant")


def _read_response(response: Any) -> tuple[int, dict[str, Any] | None]:
    status = int(getattr(response, "status", None) or response.getcode())
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        return status, None
    try:
        return status, _safe_summary(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return status, None


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise WorkflowError("worker_url_invalid")
    return value.rstrip("/")


def run_request(
    action: str,
    confirm: str,
    plan_json: str,
    base_url: str,
    token: str,
    *,
    opener: Any = None,
    temp_directory: str | None = None,
) -> Result:
    if action not in ENDPOINTS:
        raise WorkflowError("invalid_action")
    if action == "apply" and confirm != "true":
        raise WorkflowError("apply_confirmation_required")
    try:
        raw_plan = plan_json.encode("utf-8", "strict")
    except UnicodeError:
        raise WorkflowError("plan_json_invalid") from None
    if len(raw_plan) > MAX_PLAN_BYTES:
        raise WorkflowError("PLAN_INPUT_TOO_LARGE", size=len(raw_plan))
    try:
        plan = json.loads(raw_plan.decode("utf-8"), object_pairs_hook=_unique_json_object,
                          parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise WorkflowError("plan_json_invalid") from None
    if not isinstance(plan, dict) or set(plan) != {"entries"} or not isinstance(plan["entries"], list) or not plan["entries"]:
        raise WorkflowError("plan_shape_invalid")
    if not base_url or not token:
        raise WorkflowError("worker_credentials_unavailable")
    try:
        endpoint = _validate_base_url(base_url) + ENDPOINTS[action]
    except (TypeError, ValueError):
        raise WorkflowError("worker_url_invalid") from None

    private_dir = tempfile.mkdtemp(prefix="reclassification-", dir=temp_directory)
    os.chmod(private_dir, 0o700)
    plan_path = os.path.join(private_dir, "plan.json")
    try:
        fd = os.open(plan_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw_plan)
        # Embed the exact caller-supplied JSON bytes; do not reorder or rewrite entries.
        with open(plan_path, "rb") as stream:
            exact_plan = stream.read()
        suffix = b',"confirmation":"' + CONFIRMATION.encode("ascii") + b'"' if action == "apply" else b""
        body = b'{"plan":' + exact_plan + suffix + b"}"
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        client = opener or urllib.request.build_opener(_NoRedirect())
        try:
            with client.open(request, timeout=20) as response:
                status, summary = _read_response(response)
        except urllib.error.HTTPError as error:
            # Read only a bounded body for allowlisted aggregate fields; never reflect it.
            try:
                _, summary = _read_response(error)
            except (OSError, UnicodeDecodeError, ValueError):
                summary = None
            finally:
                error.close()
            return Result(False, f"worker_http_{int(error.code)}", summary or {})
        except (urllib.error.URLError, TimeoutError, OSError):
            return Result(False, "worker_transport_failed", {})
        if not 200 <= status < 300:
            return Result(False, f"worker_http_{status}", summary or {})
        if summary is None:
            return Result(False, "worker_response_invalid", {})
        return Result(True, None, summary)
    finally:
        shutil.rmtree(private_dir, ignore_errors=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, file: Any, code: int, message: str, headers: Any, new_url: str) -> None:
        return None


def _write_github_summary(action: str, result: Result) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    fields = {"action": action, **result.summary}
    lines = ["## Controlled reclassification " + ("PASS" if result.ok else "FAIL")]
    if result.category:
        lines.append("- error category: `" + result.category + "`")
    for key, value in fields.items():
        if key == "plan_sha256":
            lines.append("- plan SHA-256: `" + value + "`")
        else:
            lines.append("- " + key.replace("_", " ") + ": `" + str(value) + "`")
    with open(path, "a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def main() -> int:
    action = os.environ.get("INPUT_ACTION", "")
    confirm = os.environ.get("INPUT_CONFIRM", "false")
    plan_json = os.environ.get("INPUT_PLAN_JSON", "")
    try:
        result = run_request(action, confirm, plan_json, os.environ.get("INGEST_API_URL", ""),
                             os.environ.get("INGEST_API_TOKEN", ""))
    except WorkflowError as error:
        result = Result(False, error.category, {})
        if error.size is not None:
            result = Result(False, error.category, {"plan_input_bytes": error.size, "max_plan_bytes": MAX_PLAN_BYTES})
    except Exception:
        result = Result(False, "worker_request_failed", {})
    _write_github_summary(action, result)
    if result.ok:
        print("reclassification=PASS")
        for key, value in result.summary.items():
            print(f"{key}={value}")
        return 0
    print("reclassification=FAIL category=" + str(result.category))
    if result.category == "PLAN_INPUT_TOO_LARGE":
        print(f"plan_input_bytes={result.summary['plan_input_bytes']} max_plan_bytes={MAX_PLAN_BYTES}")
    # Non-success HTTP responses keep only filtered aggregate summary data.
    for key, value in result.summary.items():
        if key != "action":
            print(f"{key}={value}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
