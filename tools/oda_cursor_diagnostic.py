#!/usr/bin/env python3
"""Bounded, read-only ODA pagination cursor diagnostic.

Cursor IDs, timestamps, raw rows, and filter strings stay in memory and are
never included in the emitted JSON summary.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, build_opener

# Direct execution puts this file's directory on sys.path, not the repository
# root. Add the root explicitly so `python3 tools/oda_cursor_diagnostic.py` works
# without a caller-provided PYTHONPATH.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collectors.folketinget import (
    COLLECTIONS,
    ODataClient,
    ODataDeadlineError,
    ODataError,
    ODataHTTPError,
    ODataPageError,
    ODataPaginationError,
    _ODAHTTPSRedirectHandler,
)

MAX_PAGES = 5
MAX_PAGE_SIZE = 10
MAX_REQUESTS = 30
REQUEST_TIMEOUT_SECONDS = 2.0
PROCESS_TIMEOUT_SECONDS = 75


class RequestBudgetExceeded(RuntimeError):
    pass


class ProcessDeadlineExceeded(RuntimeError):
    pass


@dataclass
class CapturedResponse:
    request_filter: str
    rows: list[dict[str, object]]
    status: int
    error_category: str | None = None


class ResponseTap:
    """Transparent response wrapper that stores raw page rows in memory."""

    def __init__(self, response: object, capture: CapturedResponse) -> None:
        self._response = response
        self._capture = capture

    def __enter__(self) -> "ResponseTap":
        self._response.__enter__()  # type: ignore[attr-defined]
        return self

    def __exit__(self, *args: object) -> object:
        return self._response.__exit__(*args)  # type: ignore[attr-defined]

    def getcode(self) -> int:
        return self._response.getcode()  # type: ignore[attr-defined,no-any-return]

    def geturl(self) -> str:
        return self._response.geturl()  # type: ignore[attr-defined,no-any-return]

    def read(self, *args: object) -> bytes:
        payload = self._response.read(*args)  # type: ignore[attr-defined]
        try:
            document = json.loads(payload)
            values = document.get("value", []) if isinstance(document, dict) else []
            if isinstance(values, list) and all(isinstance(row, dict) for row in values):
                self._capture.rows = values
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            pass
        return payload


class BoundedCapture:
    """Enforce the aggregate HTTP budget and keep query evidence private."""

    def __init__(
        self,
        opener: Callable[..., object],
        *,
        max_requests: int = MAX_REQUESTS,
    ) -> None:
        self._opener = opener
        self.max_requests = max_requests
        self.request_count = 0
        self.responses: list[CapturedResponse] = []

    def open(self, request: Request, timeout: float) -> ResponseTap:
        if self.request_count >= self.max_requests:
            raise RequestBudgetExceeded
        self.request_count += 1
        query = parse_qs(urlparse(request.full_url).query)
        captured = CapturedResponse(query.get("$filter", [""])[0], [], 0)
        self.responses.append(captured)
        try:
            response = self._opener(request, timeout=timeout)
            captured.status = response.getcode()  # type: ignore[attr-defined]
            return ResponseTap(response, captured)
        except HTTPError as exc:
            captured.status = exc.code
            captured.error_category = f"HTTP_{exc.code}"
            raise
        except (TimeoutError, URLError, ConnectionError, OSError) as exc:
            captured.error_category = _error_category(exc)
            raise


def _error_category(error: BaseException | None) -> str:
    if isinstance(error, ODataHTTPError):
        return f"HTTP_{error.status_code}" if error.status_code else "HTTP_ERROR"
    if isinstance(error, HTTPError):
        return f"HTTP_{error.code}"
    if isinstance(error, ODataDeadlineError):
        return "REQUEST_DEADLINE"
    if isinstance(error, (TimeoutError, URLError, ConnectionError)):
        return "NETWORK_TIMEOUT_OR_FAILURE"
    if isinstance(error, RequestBudgetExceeded):
        return "REQUEST_BUDGET"
    if isinstance(error, ProcessDeadlineExceeded):
        return "PROCESS_DEADLINE"
    if isinstance(error, ODataPaginationError):
        message = str(error).lower()
        if "cursor did not advance" in message:
            return "CURSOR_NOT_ADVANCED"
        if "upper bound" in message:
            return "ABOVE_UPPER_BOUND"
        if "page limit" in message:
            return "PAGE_LIMIT"
        return "PAGINATION_ERROR"
    if isinstance(error, ODataPageError):
        return _error_category(error.cause) if error.cause else "PAGE_ERROR"
    if isinstance(error, ODataError):
        return type(error).__name__.upper()
    return type(error).__name__.upper() if error else "UNKNOWN"


def _cursor_precision(timestamp: str) -> int:
    return len(timestamp.rsplit(".", 1)[1]) if "." in timestamp else 0


def _response_order(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "empty"
    keys: list[tuple[datetime, int, int]] = []
    for row in rows:
        stamp = row.get("opdateringsdato")
        entity_id = row.get("id")
        if not isinstance(stamp, str) or isinstance(entity_id, bool) or not isinstance(entity_id, int):
            return "unclassifiable"
        base, dot, fraction = stamp.partition(".")
        try:
            parsed = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
            ticks = int(fraction.ljust(7, "0")) if dot else 0
        except ValueError:
            return "unclassifiable"
        keys.append((parsed, ticks, entity_id))
    if len(keys) == 1:
        return "single"
    if keys == sorted(keys):
        return "ascending"
    if keys == sorted(keys, reverse=True):
        return "descending"
    return "mixed"


def _precision_timestamp(timestamp: str, digits: int) -> str:
    base, dot, fraction = timestamp.partition(".")
    if digits == 0:
        return base
    if not dot or len(fraction) < digits:
        raise ValueError("requested precision is unavailable in the source timestamp")
    return f"{base}.{fraction[:digits]}"


def _comparison_specs(timestamp: str, cursor_id: int) -> list[tuple[str, int, str]]:
    specs: list[tuple[str, int, str]] = []
    precision = _cursor_precision(timestamp)
    literal = f"datetime'{timestamp}'"
    for operator in ("gt", "eq", "ge", "lt"):
        specs.append((f"timestamp_{operator}_exact", precision,
                      f"opdateringsdato {operator} {literal}"))
    specs.extend([
        ("compound_keyset_exact", precision,
         f"(opdateringsdato gt {literal} or (opdateringsdato eq {literal} and id gt {cursor_id}))"),
        ("compound_equal_timestamp_id_gt_exact", precision,
         f"(opdateringsdato eq {literal} and id gt {cursor_id})"),
        ("compound_cursor_identity_exact", precision,
         f"(opdateringsdato eq {literal} and id eq {cursor_id})"),
    ])
    if precision == 7:
        for digits in range(8):
            variant = _precision_timestamp(timestamp, digits)
            variant_literal = f"datetime'{variant}'"
            specs.append((f"timestamp_gt_precision_{digits}", digits,
                          f"opdateringsdato gt {variant_literal}"))
            specs.append((f"compound_keyset_precision_{digits}", digits,
                          f"(opdateringsdato gt {variant_literal} or "
                          f"(opdateringsdato eq {variant_literal} and id gt {cursor_id}))"))
    return specs


def _cursor_in_rows(rows: list[dict[str, object]], timestamp: str, entity_id: int) -> bool:
    return any(row.get("opdateringsdato") == timestamp and row.get("id") == entity_id for row in rows)


def run_diagnostic(
    opener: Callable[..., object],
    *,
    entity: str = "Dokument",
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    total_timeout: float = 65.0,
    max_requests: int = MAX_REQUESTS,
) -> dict[str, object]:
    """Run bounded scan then compare filters; returned object contains summaries only."""
    if entity not in COLLECTIONS or not (0 < max_requests <= MAX_REQUESTS):
        raise ValueError("unsupported diagnostic configuration")
    capture = BoundedCapture(opener, max_requests=max_requests)
    client = ODataClient(timeout=timeout, total_timeout=total_timeout, retries=0,
                         page_size=MAX_PAGE_SIZE, opener=capture.open)
    scan_error: BaseException | None = None
    try:
        client.scan(entity, page_size=MAX_PAGE_SIZE, max_pages=MAX_PAGES,
                    select="id,opdateringsdato")
    except (ODataError, RequestBudgetExceeded, TimeoutError, URLError, ConnectionError, OSError) as exc:
        scan_error = exc

    last_scan_response = capture.responses[-1] if capture.responses else None
    partial = scan_error.partial_result if isinstance(scan_error, ODataPageError) else None
    cursor = partial.metadata.last_successful_cursor if partial else None
    cursor_row_repeated = bool(
        cursor and last_scan_response and
        _cursor_in_rows(last_scan_response.rows, cursor.timestamp, cursor.entity_id)
    )
    # Keep filter and ordering evidence private alongside the cursor.
    private_evidence = {
        "filter": last_scan_response.request_filter if last_scan_response else "",
        "response_ordering": _response_order(last_scan_response.rows) if last_scan_response else "unavailable",
    }
    scan_summary: dict[str, object] = {
        "error_category": _error_category(scan_error) if scan_error else None,
        "cursor_row_repeated": cursor_row_repeated,
    }
    comparisons: list[dict[str, object]] = []
    reproducible: bool | None = None

    if cursor is not None:
        # The exact query filter and cursor are deliberately retained only in
        # `capture` and local variables; no raw values leave this function.
        private_evidence.update({"cursor_timestamp": cursor.timestamp, "cursor_id": cursor.entity_id})
        precision = _cursor_precision(cursor.timestamp)
        if precision == 7:
            for label, digits, filter_value in _comparison_specs(cursor.timestamp, cursor.entity_id):
                try:
                    rows = client.fetch_page(entity, {
                        "$filter": filter_value,
                        "$select": "id,opdateringsdato",
                        "$orderby": "opdateringsdato asc,id asc",
                        "$top": MAX_PAGE_SIZE,
                    })
                    outcome = "cursor_present" if _cursor_in_rows(rows, cursor.timestamp, cursor.entity_id) else "cursor_absent"
                    category = None
                except (ODataError, RequestBudgetExceeded, TimeoutError, URLError, ConnectionError, OSError) as exc:
                    outcome = "error"
                    category = _error_category(exc)
                comparisons.append({"comparison": label, "precision_digits": digits,
                                    "outcome": outcome, "error_category": category})
            exact_gt = next((c["outcome"] for c in comparisons if c["comparison"] == "timestamp_gt_exact"), None)
            exact_eq = next((c["outcome"] for c in comparisons if c["comparison"] == "timestamp_eq_exact"), None)
            if exact_gt in {"cursor_present", "cursor_absent"} and exact_eq in {"cursor_present", "cursor_absent"}:
                reproducible = exact_gt == "cursor_present" and exact_eq == "cursor_absent"
        else:
            scan_summary["cursor_precision_digits"] = precision
            scan_summary["comparison_status"] = "inconclusive_cursor_not_seven_digit"
    else:
        scan_summary["error_category"] = scan_summary["error_category"] or "NO_CAPTURED_CURSOR"

    return {
        "scan": scan_summary,
        "comparisons": comparisons,
        "behavior_reproducible": reproducible,
    }


def _alarm_handler(_signum: int, _frame: object) -> None:
    raise ProcessDeadlineExceeded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity", default="Dokument", choices=sorted(COLLECTIONS))
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(PROCESS_TIMEOUT_SECONDS)
    try:
        official_opener = build_opener(_ODAHTTPSRedirectHandler())
        result = run_diagnostic(official_opener.open, entity=args.entity)
    except ProcessDeadlineExceeded:
        result = {
            "scan": {"error_category": "PROCESS_DEADLINE", "cursor_row_repeated": False},
            "comparisons": [],
            "behavior_reproducible": None,
        }
    except Exception as exc:
        result = {
            "scan": {"error_category": _error_category(exc), "cursor_row_repeated": False},
            "comparisons": [],
            "behavior_reproducible": None,
        }
    finally:
        signal.alarm(0)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["behavior_reproducible"] is not None else 2


if __name__ == "__main__":
    sys.exit(main())
