from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import patch

from tools import reclassification_workflow as workflow


PLAN = '{ "entries" : [ {"external_id":"70190", "new_relevance_reason":"private reason"} ] }'
TOKEN = "private-bearer-token"
SUMMARY = {"status": "PREVIEW", "plan_sha256": "a" * 64, "expected_count": 1,
           "ready": 1, "already_applied": 0, "stale": 0, "missing": 0, "invalid": 0,
           "blocked": 0, "conflicts": [{"external_id": "70190", "private": "must not leak"}],
           "telegram_id": "secret-user", "debug": "source payload"}


class Response:
    def __init__(self, status: int, body: object):
        self.status = status
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, amount: int = -1):
        return self.body if amount < 0 else self.body[:amount]

    def getcode(self):
        return self.status


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


class ReclassificationWorkflowTests(unittest.TestCase):
    def test_preview_posts_only_fixed_preview_endpoint_with_exact_plan_json(self):
        opener = Opener(Response(200, SUMMARY))
        result = workflow.run_request("preview", "false", PLAN, "https://worker.example/", TOKEN,
                                      opener=opener)
        request, timeout = opener.calls[0]
        self.assertTrue(result.ok)
        self.assertEqual(request.full_url, "https://worker.example/internal/reclassification/preview")
        self.assertEqual(json.loads(request.data), {"plan": json.loads(PLAN)})
        self.assertTrue(request.data.endswith(PLAN.encode() + b"}"))
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(timeout, 20)
        self.assertNotIn("conflicts", result.summary)
        self.assertNotIn("telegram_id", result.summary)

    def test_apply_posts_only_fixed_apply_endpoint_and_confirmation(self):
        opener = Opener(Response(200, {**SUMMARY, "status": "APPLIED"}))
        result = workflow.run_request("apply", "true", PLAN, "https://worker.example", TOKEN, opener=opener)
        request, _ = opener.calls[0]
        self.assertTrue(result.ok)
        self.assertEqual(request.full_url, "https://worker.example/internal/reclassification/apply")
        self.assertEqual(json.loads(request.data), {"plan": json.loads(PLAN),
                         "confirmation": "APPLY_PINNED_RECLASSIFICATION_PLAN"})
        self.assertTrue(request.data.startswith(b'{"plan":' + PLAN.encode()))

    def test_apply_without_confirmation_fails_before_http(self):
        opener = Opener(Response(200, SUMMARY))
        with self.assertRaisesRegex(workflow.WorkflowError, "apply_confirmation_required"):
            workflow.run_request("apply", "false", PLAN, "https://worker.example", TOKEN, opener=opener)
        self.assertEqual(opener.calls, [])

    def test_plan_limit_uses_utf8_size_and_fails_before_http(self):
        opener = Opener(Response(200, SUMMARY))
        plan = '{"entries":["' + "é" * 30_721 + '"]}'
        with self.assertRaises(workflow.WorkflowError) as error:
            workflow.run_request("preview", "false", plan, "https://worker.example", TOKEN, opener=opener)
        self.assertEqual(error.exception.category, "PLAN_INPUT_TOO_LARGE")
        self.assertEqual(error.exception.size, len(plan.encode("utf-8")))
        self.assertEqual(opener.calls, [])

    def test_json_top_level_shape_is_validated_without_reordering(self):
        for plan in ('[]', '{"entries":[]}', '{"entries":[{}],"extra":1}',
                     '{"entries":[{}],"entries":[{}]}', '{"entries":[NaN]}', "not json"):
            with self.subTest(plan=plan), self.assertRaises(workflow.WorkflowError):
                workflow.run_request("preview", "false", plan, "https://worker.example", TOKEN,
                                     opener=Opener(Response(200, SUMMARY)))

    def test_http_failure_filters_body_and_preserves_only_safe_counts(self):
        body = json.dumps({**SUMMARY, "description": TOKEN, "message": "private detail"}).encode()
        error = urllib.error.HTTPError("https://worker.example/private", 409, "conflict", {}, io.BytesIO(body))
        result = workflow.run_request("apply", "true", PLAN, "https://worker.example", TOKEN, opener=Opener(error=error))
        self.assertFalse(result.ok)
        self.assertEqual(result.category, "worker_http_409")
        self.assertEqual(result.summary["ready"], 1)
        rendered = json.dumps(result.summary)
        for private in (TOKEN, "private reason", "private detail", "must not leak", "secret-user"):
            self.assertNotIn(private, rendered)

    def test_transport_failure_and_malformed_response_are_bounded(self):
        result = workflow.run_request("preview", "false", PLAN, "https://worker.example", TOKEN,
                                      opener=Opener(error=urllib.error.URLError("private network detail")))
        self.assertEqual(result.category, "worker_transport_failed")
        malformed = workflow.run_request("preview", "false", PLAN, "https://worker.example", TOKEN,
                                          opener=Opener(Response(200, {"private": "payload"})))
        self.assertEqual(malformed.category, "worker_response_invalid")

    def test_temp_plan_file_is_private_and_removed_after_request(self):
        with tempfile.TemporaryDirectory() as directory:
            original = workflow.tempfile.mkdtemp
            original_open = workflow.os.open
            captured = []
            modes = []

            def recording_mkdtemp(*args, **kwargs):
                path = original(*args, **kwargs)
                captured.append(path)
                return path

            def recording_open(path, flags, mode=0o777, *args, **kwargs):
                fd = original_open(path, flags, mode, *args, **kwargs)
                modes.append((path, stat.S_IMODE(os.fstat(fd).st_mode)))
                return fd

            with patch.object(workflow.tempfile, "mkdtemp", side_effect=recording_mkdtemp), \
                 patch.object(workflow.os, "open", side_effect=recording_open):
                result = workflow.run_request("preview", "false", PLAN, "https://worker.example", TOKEN,
                                              opener=Opener(Response(200, SUMMARY)), temp_directory=directory)
            self.assertTrue(result.ok)
            self.assertEqual(len(captured), 1)
            self.assertEqual(modes[0][1], 0o600)
            self.assertFalse(os.path.exists(captured[0]))
            self.assertEqual(os.listdir(directory), [])

    def test_temp_plan_is_removed_after_http_failure_too(self):
        with tempfile.TemporaryDirectory() as directory:
            body = json.dumps({**SUMMARY, "description": "private detail"}).encode()
            error = urllib.error.HTTPError("https://worker.example", 409, "conflict", {}, io.BytesIO(body))
            result = workflow.run_request("apply", "true", PLAN, "https://worker.example", TOKEN,
                                          opener=Opener(error=error), temp_directory=directory)
            self.assertEqual(result.category, "worker_http_409")
            self.assertEqual(os.listdir(directory), [])

    def test_non_2xx_cli_writes_only_filtered_summary_and_fails(self):
        body = json.dumps({**SUMMARY, "description": "private response detail"}).encode()
        error = urllib.error.HTTPError("https://worker.example", 409, "conflict", {}, io.BytesIO(body))
        with tempfile.NamedTemporaryFile(mode="r+", encoding="utf-8") as summary:
            output = io.StringIO()
            with patch.dict(os.environ, {"INPUT_ACTION": "apply", "INPUT_CONFIRM": "true",
                 "INPUT_PLAN_JSON": PLAN, "INGEST_API_URL": "https://worker.example", "INGEST_API_TOKEN": TOKEN,
                 "GITHUB_STEP_SUMMARY": summary.name}), patch.object(workflow.urllib.request, "build_opener",
                 return_value=Opener(error=error)), redirect_stdout(output):
                self.assertEqual(workflow.main(), 1)
            summary.seek(0)
            rendered = output.getvalue() + summary.read()
            for private in (TOKEN, "private reason", "private response detail", "must not leak", "70190"):
                self.assertNotIn(private, rendered)
            self.assertIn("worker_http_409", rendered)
            self.assertIn("ready", rendered)

    def test_cli_summary_and_stdout_never_print_plan_conflicts_or_token(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"INPUT_ACTION": "preview", "INPUT_CONFIRM": "false",
             "INPUT_PLAN_JSON": PLAN, "INGEST_API_URL": "https://worker.example", "INGEST_API_TOKEN": TOKEN,
             "GITHUB_STEP_SUMMARY": ""}), patch.object(workflow.urllib.request, "build_opener",
             return_value=Opener(Response(200, SUMMARY))), redirect_stdout(output):
            self.assertEqual(workflow.main(), 0)
        rendered = output.getvalue()
        for private in ("private reason", "private-bearer-token", "70190", "must not leak", "secret-user", "source payload"):
            self.assertNotIn(private, rendered)
        self.assertIn("ready=1", rendered)

    def test_workflow_is_dispatch_only_confirmed_and_serialized(self):
        path = ".github/workflows/reclassification-maintenance.yml"
        with open(path, encoding="utf-8") as stream:
            content = stream.read()
        try:
            import yaml
        except ImportError:
            self.assertTrue(content.startswith("name: Controlled production reclassification\n\non:\n  workflow_dispatch:"))
        else:
            parsed = yaml.load(content, Loader=yaml.BaseLoader)
            self.assertEqual(set(parsed["on"]), {"workflow_dispatch"})
            self.assertEqual(parsed["concurrency"], {"group": "production-reclassification", "cancel-in-progress": "false"})
            inputs = parsed["on"]["workflow_dispatch"]["inputs"]
            self.assertEqual(set(inputs), {"action", "plan_json", "confirm"})
        self.assertIn("permissions:\n  contents: read", content)
        self.assertIn("INPUT_CONFIRM: ${{ inputs.confirm }}", content)
        self.assertNotIn("set -x", content)
        self.assertIn("set +x", content)
        self.assertNotIn("TELEGRAM", content)
        for trigger in ("schedule:", "push:", "pull_request:", "workflow_run:"):
            self.assertNotIn(trigger, content)
        inputs = content.split("    inputs:", 1)[1].split("\n\npermissions:", 1)[0]
        for forbidden_input in ("INGEST_API_URL", "INGEST_API_TOKEN", "expected_count", "plan_sha256", "confirmation"):
            self.assertNotIn(forbidden_input, inputs)
        self.assertIn("INGEST_API_URL: ${{ vars.INGEST_API_URL }}", content)
        self.assertIn("INGEST_API_TOKEN: ${{ secrets.INGEST_API_TOKEN }}", content)


if __name__ == "__main__":
    unittest.main()
