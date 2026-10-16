"""Bounded HTTP transport and keyset paging for Folketinget OData 3.0."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPException
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

ODA_ROOT = "https://oda.ft.dk/api/"
USER_AGENT = "DenmarkTgBot-FolketingetCollector/1.0"
DEFAULT_TIMEOUT = 15.0
DEFAULT_RETRIES = 2
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
COLLECTIONS = frozenset(
    {"Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus"}
)
_ALLOWED_PARAMETERS = frozenset({"$filter", "$select", "$orderby", "$top"})
_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,7}))?$"
)
LOGGER = logging.getLogger(__name__)


class ODataError(Exception):
    """Base class for concise ODA transport and pagination failures."""


class ODataNetworkError(ODataError):
    pass


class ODataHTTPError(ODataError):
    pass


class ODataJSONError(ODataError):
    pass


class ODataStructureError(ODataError):
    pass


class ODataCursorError(ODataError):
    pass


class ODataPaginationError(ODataError):
    pass


class ODataPageError(ODataError):
    """A failed page with an explicitly incomplete, inspectable result."""

    def __init__(self, page_number: int, result: "ScanResult", reason: str) -> None:
        self.page_number = page_number
        self.partial_result = result
        super().__init__(f"ODA scan failed at page {page_number}: {reason}")


@dataclass(frozen=True, order=True)
class Cursor:
    """Opaque ODA datetime text plus the entity's numeric ID."""

    timestamp: str
    entity_id: int


@dataclass(frozen=True)
class ScanMetadata:
    collection: str
    page_count: int
    record_count: int
    upper_bound: Cursor | None
    last_successful_cursor: Cursor | None
    complete: bool
    request_count: int


@dataclass(frozen=True)
class ScanResult:
    records: tuple[dict[str, object], ...]
    metadata: ScanMetadata


def _validate_collection(collection: str) -> None:
    if collection not in COLLECTIONS:
        raise ValueError("unsupported ODA collection")


def _is_official_https_url(url: str) -> bool:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "oda.ft.dk"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
    )


def build_url(collection: str, parameters: Mapping[str, object]) -> str:
    """Build a bounded URL for an allowlisted ODA collection."""
    _validate_collection(collection)
    if not parameters or any(key not in _ALLOWED_PARAMETERS for key in parameters):
        raise ValueError("unsupported or missing OData query parameter")
    if "$top" not in parameters:
        raise ValueError("$top is required to keep ODA requests bounded")
    try:
        top = int(parameters["$top"])
    except (TypeError, ValueError) as exc:
        raise ValueError("$top must be a positive integer") from exc
    if top < 1 or top > MAX_PAGE_SIZE:
        raise ValueError(f"$top must be between 1 and {MAX_PAGE_SIZE}")
    pairs: list[tuple[str, str]] = []
    for key, value in parameters.items():
        if value is None:
            continue
        if key == "$top":
            encoded_value = str(top)
        elif not isinstance(value, str) or not value:
            raise ValueError(f"{key} must be a non-empty string")
        else:
            encoded_value = value
        pairs.append((key, encoded_value))
    return f"{ODA_ROOT}{collection}?{urlencode(pairs)}"


def _timestamp_key(value: object) -> tuple[datetime, int]:
    if not isinstance(value, str):
        raise ODataCursorError("opdateringsdato is missing or is not text")
    match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        raise ODataCursorError("opdateringsdato has an unsupported format")
    base, fraction = match.groups()
    try:
        parsed = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise ODataCursorError("opdateringsdato is not a valid calendar time") from exc
    # Pad only the comparison key. The original source text remains untouched.
    fractional_ticks = int((fraction or "").ljust(7, "0"))
    return parsed, fractional_ticks


def _cursor_from_record(record: Mapping[str, object]) -> Cursor:
    timestamp = record.get("opdateringsdato")
    _timestamp_key(timestamp)
    entity_id = record.get("id")
    if isinstance(entity_id, bool) or not isinstance(entity_id, int):
        raise ODataCursorError("entity id is missing or is not an integer")
    return Cursor(timestamp, entity_id)  # type: ignore[arg-type]


def _cursor_key(cursor: Cursor) -> tuple[datetime, int, int]:
    date_key, fraction = _timestamp_key(cursor.timestamp)
    return date_key, fraction, cursor.entity_id


def _date_literal(value: str) -> str:
    _timestamp_key(value)
    return f"datetime'{value}'"


def _combine_filters(*filters: str | None) -> str | None:
    selected = [f"({item})" for item in filters if item]
    return " and ".join(selected) or None


def _upper_filter(cursor: Cursor) -> str:
    literal = _date_literal(cursor.timestamp)
    return (
        f"(opdateringsdato lt {literal} or "
        f"(opdateringsdato eq {literal} and id le {cursor.entity_id}))"
    )


def _after_filter(cursor: Cursor) -> str:
    literal = _date_literal(cursor.timestamp)
    return (
        f"(opdateringsdato gt {literal} or "
        f"(opdateringsdato eq {literal} and id gt {cursor.entity_id}))"
    )


class _ODAHTTPSRedirectHandler(HTTPRedirectHandler):
    """Permit only HTTPS redirects that remain on the official ODA host."""

    def redirect_request(
        self,
        request: Request,
        file: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> Request | None:
        if not _is_official_https_url(new_url):
            raise ODataHTTPError("ODA returned an unsupported redirect")
        return super().redirect_request(request, file, code, message, headers, new_url)


class ODataClient:
    """Read-only ODA client with bounded requests and snapshot-bounded scans."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        page_size: int = DEFAULT_PAGE_SIZE,
        opener: Callable[..., object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if retries < 0 or retries > 5:
            raise ValueError("retries must be between 0 and 5")
        if page_size < 1 or page_size > MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        self.timeout = timeout
        self.retries = retries
        self.page_size = page_size
        self._opener = opener or build_opener(_ODAHTTPSRedirectHandler()).open
        self._sleep = sleep

    def fetch_page(self, collection: str, parameters: Mapping[str, object]) -> list[dict[str, object]]:
        """Fetch one bounded page and return raw entity dictionaries."""
        url = build_url(collection, parameters)
        if not _is_official_https_url(url):
            raise ValueError("ODA requests must target the official HTTPS host")
        request = Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        attempts = self.retries + 1
        last_failure = "request failed"
        for attempt in range(1, attempts + 1):
            retryable = False
            try:
                response = self._opener(request, timeout=self.timeout)
                with response as result:  # type: ignore[attr-defined]
                    status = result.getcode()
                    final_url = result.geturl()
                    if not _is_official_https_url(final_url):
                        raise ODataHTTPError("ODA response used an unsupported redirect")
                    if status != 200:
                        last_failure = f"HTTP {status}"
                        retryable = status == 429 or 500 <= status <= 599
                    else:
                        payload = result.read()
                        if not payload:
                            raise ODataStructureError("ODA returned an empty HTTP response")
                        try:
                            document = json.loads(payload)
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise ODataJSONError("ODA returned invalid JSON") from exc
                        if not isinstance(document, dict) or not isinstance(document.get("value"), list):
                            raise ODataStructureError("ODA JSON has no value array")
                        rows = document["value"]
                        if any(not isinstance(row, dict) for row in rows):
                            raise ODataStructureError("ODA value array contains a non-object entity")
                        return rows
            except HTTPError as exc:
                last_failure = f"HTTP {exc.code}"
                retryable = exc.code == 429 or 500 <= exc.code <= 599
            except (URLError, TimeoutError, ConnectionError, OSError, HTTPException) as exc:
                last_failure = type(exc).__name__
                retryable = True
            except ODataError:
                raise

            if not retryable or attempt == attempts:
                error_type = ODataHTTPError if last_failure.startswith("HTTP ") else ODataNetworkError
                raise error_type(f"ODA {collection} request failed: {last_failure}")
            delay = min(0.25 * (2 ** (attempt - 1)), 2.0)
            LOGGER.warning("ODA %s request attempt %d/%d failed (%s); retrying", collection, attempt, attempts, last_failure)
            self._sleep(delay)
        raise ODataNetworkError(f"ODA {collection} request failed")  # unreachable guard

    def scan(
        self,
        collection: str,
        *,
        filter: str | None = None,
        select: str | None = None,
        page_size: int | None = None,
    ) -> ScanResult:
        """Read a collection in ascending (opdateringsdato, id) order.

        ``filter`` is an OData filter expression and is URL-encoded as a whole.
        It should be constructed from trusted, correctly escaped OData literals.
        """
        _validate_collection(collection)
        size = self.page_size if page_size is None else page_size
        if size < 1 or size > MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        if select is not None and not {"id", "opdateringsdato"}.issubset(
            {part.strip() for part in select.split(",")}
        ):
            raise ValueError("scan select must include id and opdateringsdato")

        output: dict[int, tuple[tuple[datetime, int, int], dict[str, object]]] = {}
        pages = 0
        records_fetched = 0
        requests = 0
        last_cursor: Cursor | None = None
        upper: Cursor | None = None

        def result(complete: bool) -> ScanResult:
            ordered = tuple(item[1] for item in sorted(output.values(), key=lambda item: item[0]))
            return ScanResult(
                ordered,
                ScanMetadata(collection, pages, records_fetched, upper, last_cursor, complete, requests),
            )

        try:
            high_rows = self.fetch_page(
                collection,
                {
                    "$filter": filter,
                    "$select": "id,opdateringsdato",
                    "$orderby": "opdateringsdato desc,id desc",
                    "$top": 1,
                },
            )
            requests += 1
            if not high_rows:
                return result(True)
            upper = _cursor_from_record(high_rows[0])

            while True:
                page_filter = _combine_filters(
                    filter,
                    _upper_filter(upper),
                    _after_filter(last_cursor) if last_cursor is not None else None,
                )
                params: dict[str, object] = {
                    "$filter": page_filter,
                    "$orderby": "opdateringsdato asc,id asc",
                    "$top": size,
                }
                if select is not None:
                    params["$select"] = select
                try:
                    rows = self.fetch_page(collection, params)
                    requests += 1
                except ODataError as exc:
                    requests += 1
                    raise ODataPageError(pages + 1, result(False), str(exc)) from exc
                if not rows:
                    return result(True)
                pages += 1
                records_fetched += len(rows)
                for row in rows:
                    cursor = _cursor_from_record(row)
                    cursor_key = _cursor_key(cursor)
                    if cursor_key > _cursor_key(upper):
                        raise ODataPaginationError("ODA page exceeded the captured upper bound")
                    if last_cursor is not None and cursor_key <= _cursor_key(last_cursor):
                        raise ODataPaginationError("ODA page cursor did not advance")
                    last_cursor = cursor
                    existing = output.get(cursor.entity_id)
                    if existing is None or cursor_key > existing[0]:
                        output[cursor.entity_id] = (cursor_key, row)
                if last_cursor == upper:
                    return result(True)
                # Deliberately continue after short pages: the upper-bounded range,
                # not the page length, determines completion.
        except ODataPageError:
            raise
        except (ODataCursorError, ODataPaginationError):
            raise
        except ODataError as exc:
            requests += 1
            raise ODataPageError(0, result(False), str(exc)) from exc


__all__ = [
    "COLLECTIONS",
    "Cursor",
    "ODataClient",
    "ODataCursorError",
    "ODataError",
    "ODataHTTPError",
    "ODataJSONError",
    "ODataNetworkError",
    "ODataPageError",
    "ODataPaginationError",
    "ODataStructureError",
    "ScanMetadata",
    "ScanResult",
    "build_url",
]
