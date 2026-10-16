from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tools.oda_cursor_diagnostic import run_diagnostic


CURSOR_TIMESTAMP = "2026-10-14T08:46:08.6566667"
CURSOR_ID = 987654321
ROOT = Path(__file__).resolve().parents[1]


class FixtureResponse:
    def __init__(self, rows: list[dict[str, object]], url: str) -> None:
        self._body = json.dumps({"value": rows}).encode()
        self._url = url

    def __enter__(self) -> "FixtureResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def getcode(self) -> int:
        return 200

    def geturl(self) -> str:
        return self._url

    def read(self, *_: object) -> bytes:
        return self._body


def fixture_row(entity_id: int, timestamp: str) -> dict[str, object]:
    return {"id": entity_id, "opdateringsdato": timestamp}


class OdaCursorDiagnosticTests(unittest.TestCase):
    def test_repeated_cursor_fixture_runs_bounded_comparisons_without_leaking_cursor(self) -> None:
        cursor = fixture_row(CURSOR_ID, CURSOR_TIMESTAMP)
        upper = fixture_row(CURSOR_ID + 1, "2026-10-14T08:46:08.6566668")
        calls = 0

        def fixture_opener(request: object, timeout: float) -> FixtureResponse:
            nonlocal calls
            calls += 1
            url = request.full_url  # type: ignore[attr-defined]
            query = parse_qs(urlparse(url).query)
            filter_value = query.get("$filter", [""])[0]
            order = query.get("$orderby", [""])[0]
            if order == "opdateringsdato desc,id desc":
                rows = [upper]
            elif query.get("$top") == ["10"] and "opdateringsdato lt datetime'" in filter_value and "id le" in filter_value:
                if "id gt" in filter_value:
                    rows = [cursor]
                else:
                    rows = [cursor]
            elif "opdateringsdato eq datetime'" in filter_value and "id gt" in filter_value and "opdateringsdato gt" not in filter_value:
                rows = []
            elif "opdateringsdato eq datetime'" in filter_value and "id eq" in filter_value:
                rows = []
            elif "opdateringsdato gt datetime'" in filter_value:
                rows = [cursor]
            elif "opdateringsdato ge datetime'" in filter_value:
                rows = [cursor]
            else:
                rows = []
            return FixtureResponse(rows, url)

        summary = run_diagnostic(fixture_opener)
        rendered = json.dumps(summary)
        self.assertEqual(calls, 26)
        self.assertLessEqual(calls, 30)
        self.assertEqual(summary["scan"]["cursor_row_repeated"], True)  # type: ignore[index]
        self.assertEqual(summary["scan"]["error_category"], "CURSOR_NOT_ADVANCED")  # type: ignore[index]
        self.assertEqual(summary["behavior_reproducible"], True)
        comparisons = summary["comparisons"]
        self.assertEqual(len(comparisons), 23)  # type: ignore[arg-type]
        by_label = {item["comparison"]: item for item in comparisons}  # type: ignore[union-attr]
        self.assertEqual(by_label["timestamp_gt_exact"]["outcome"], "cursor_present")
        self.assertEqual(by_label["timestamp_eq_exact"]["outcome"], "cursor_absent")
        self.assertEqual(by_label["timestamp_ge_exact"]["outcome"], "cursor_present")
        self.assertEqual(by_label["timestamp_lt_exact"]["outcome"], "cursor_absent")
        self.assertEqual(by_label["compound_keyset_exact"]["outcome"], "cursor_present")
        self.assertEqual(by_label["compound_cursor_identity_exact"]["outcome"], "cursor_absent")
        for digits in range(8):
            self.assertEqual(by_label[f"timestamp_gt_precision_{digits}"]["outcome"], "cursor_present")
            self.assertEqual(by_label[f"compound_keyset_precision_{digits}"]["outcome"], "cursor_present")
        precision_sweep = {
            item["precision_digits"] for item in comparisons  # type: ignore[union-attr]
            if str(item["comparison"]).endswith(tuple(f"_{n}" for n in range(8)))
        }
        self.assertEqual(precision_sweep, set(range(8)))
        self.assertNotIn(CURSOR_TIMESTAMP, rendered)
        self.assertNotIn(str(CURSOR_ID), rendered)
        self.assertNotIn("datetime'", rendered)

    def test_cli_help_runs_directly_without_pythonpath(self) -> None:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "tools/oda_cursor_diagnostic.py", "--help"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--entity", result.stdout)

    def test_request_budget_stops_http_calls_at_configured_limit(self) -> None:
        upper = fixture_row(2, "2026-10-14T08:46:08.6566668")
        calls = 0

        def fixture_opener(request: object, timeout: float) -> FixtureResponse:
            nonlocal calls
            calls += 1
            return FixtureResponse([upper], request.full_url)  # type: ignore[attr-defined]

        summary = run_diagnostic(fixture_opener, max_requests=2)
        self.assertEqual(calls, 2)
        self.assertLessEqual(calls, 30)
        self.assertEqual(summary["behavior_reproducible"], None)

    def test_page_and_request_limits_hold_at_worst_case_scan_boundary(self) -> None:
        call_count = 0

        def fixture_opener(request: object, timeout: float) -> FixtureResponse:
            nonlocal call_count
            call_count += 1
            url = request.full_url  # type: ignore[attr-defined]
            query = parse_qs(urlparse(url).query)
            if query.get("$orderby") == ["opdateringsdato desc,id desc"]:
                rows = [fixture_row(51, "2026-10-14T08:46:08.6566668")]
            elif call_count <= 6:
                page = call_count - 2
                rows = [fixture_row(i, CURSOR_TIMESTAMP) for i in range(page * 10 + 1, page * 10 + 11)]
            else:
                rows = []
            return FixtureResponse(rows, url)

        summary = run_diagnostic(fixture_opener)
        self.assertEqual(call_count, 29)
        self.assertLessEqual(call_count, 30)
        self.assertEqual(summary["scan"]["error_category"], "PAGE_LIMIT")  # type: ignore[index]
        self.assertEqual(len(summary["comparisons"]), 23)  # type: ignore[arg-type]

    def test_completed_scan_without_failed_cursor_is_inconclusive(self) -> None:
        upper = fixture_row(2, "2026-10-14T08:46:08.6566668")

        def fixture_opener(request: object, timeout: float) -> FixtureResponse:
            url = request.full_url  # type: ignore[attr-defined]
            query = parse_qs(urlparse(url).query)
            if query.get("$orderby") == ["opdateringsdato desc,id desc"]:
                return FixtureResponse([upper], url)
            return FixtureResponse([upper], url)

        summary = run_diagnostic(fixture_opener)
        self.assertEqual(summary["scan"]["error_category"], "NO_CAPTURED_CURSOR")  # type: ignore[index]
        self.assertEqual(summary["comparisons"], [])
        self.assertIsNone(summary["behavior_reproducible"])


if __name__ == "__main__":
    unittest.main()
