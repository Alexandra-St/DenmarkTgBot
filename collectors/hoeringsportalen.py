"""Fetch and normalize Høringsportalen ATOM consultation feeds."""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.client import HTTPException
from html.parser import HTMLParser
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

LOGGER = logging.getLogger(__name__)
ATOM_NS = "http://www.w3.org/2005/Atom"
ATOM = f"{{{ATOM_NS}}}"
USER_AGENT = "DenmarkTgBot-HoeringsportalenCollector/1.0"
DEFAULT_TIMEOUT = 15
DEFAULT_RETRIES = 2


@dataclass(frozen=True)
class FeedConfig:
    code: str
    url: str


FEEDS = (
    FeedConfig(
        "immigration_ministry",
        "https://hoeringsportalen.dk/Syndication/HearingsByAuthorityFeed?authorityId=1745",
    ),
    FeedConfig(
        "immigration_category",
        "https://hoeringsportalen.dk/Syndication/HearingsByFormAreaFeed?formAreaId=37",
    ),
)


@dataclass(frozen=True)
class Consultation:
    external_id: str
    source: str
    title: str | None
    url: str | None
    updated_at: str | None
    author: str | None
    content_text: str | None
    feed_codes: tuple[str, ...]
    updated_at_raw: str | None = None

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["feed_codes"] = list(self.feed_codes)
        return result


@dataclass(frozen=True)
class ParseResult:
    entries: tuple[Consultation, ...]
    entries_seen: int
    invalid_entries: int
    invalid_timestamps: int


class CollectorError(Exception):
    """Base class for concise, user-facing collector failures."""


class FeedFetchError(CollectorError):
    pass


class EmptyResponseError(FeedFetchError):
    pass


class FeedParseError(CollectorError):
    pass


class _HTTPSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request: Request, file: object, code: int, message: str,
                         headers: object, new_url: str) -> Request | None:
        if urlparse(new_url).scheme.lower() != "https":
            raise FeedFetchError("Feed redirected to a non-HTTPS URL")
        return super().redirect_request(request, file, code, message, headers, new_url)


class _ReadableHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self._hidden_depth += 1
        elif tag.lower() in {"br", "p", "div", "li", "tr"} and not self._hidden_depth:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._hidden_depth:
            self._hidden_depth -= 1
        elif tag.lower() in {"p", "div", "li", "tr"} and not self._hidden_depth:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self.parts.append(data)


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _content_text(content: ET.Element | None) -> str | None:
    if content is None:
        return None
    raw = "".join(content.itertext())
    if content.attrib.get("type", "text").lower() in {"html", "xhtml"}:
        parser = _ReadableHTML()
        parser.feed(raw)
        parser.close()
        raw = " ".join(parser.parts)
    return _clean_text(raw)


def _parse_updated(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            raise ValueError("timestamp has no timezone")
        utc_value = parsed.astimezone(timezone.utc)
    except ValueError:
        return None
    return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp_key(value: str | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _external_id(uri: str) -> str:
    parsed = urlparse(uri)
    match = re.fullmatch(r"/Hearing/Details/(\d+)/?", parsed.path, re.IGNORECASE)
    return match.group(1) if match else uri


def parse_feed(xml_data: bytes | str, feed_code: str) -> ParseResult:
    """Parse one ATOM document; malformed individual entries are reported and skipped."""
    try:
        root = ET.fromstring(xml_data)
    except (ET.ParseError, ValueError) as exc:
        raise FeedParseError("invalid XML document") from exc
    if root.tag != f"{ATOM}feed":
        raise FeedParseError("document root is not an ATOM feed")

    parsed_entries: list[Consultation] = []
    invalid_entries = 0
    invalid_timestamps = 0
    for index, entry in enumerate(root.findall(f"{ATOM}entry"), start=1):
        uri = _clean_text(entry.findtext(f"{ATOM}id"))
        title = _clean_text(entry.findtext(f"{ATOM}title"))
        if not uri:
            LOGGER.warning("Feed %s entry %d skipped: missing entry/id", feed_code, index)
            invalid_entries += 1
            continue
        if not title:
            LOGGER.warning("Feed %s entry %d skipped: missing entry/title", feed_code, index)
            invalid_entries += 1
            continue

        updated_raw = _clean_text(entry.findtext(f"{ATOM}updated"))
        updated_at = _parse_updated(updated_raw)
        if updated_raw and updated_at is None:
            LOGGER.warning("Feed %s entry %s has an invalid updated timestamp", feed_code, _external_id(uri))
            invalid_timestamps += 1

        author = _clean_text(entry.findtext(f"{ATOM}author/{ATOM}name"))
        alternate = next(
            (link.attrib.get("href") for link in entry.findall(f"{ATOM}link")
             if link.attrib.get("rel") == "alternate" and link.attrib.get("href")),
            None,
        )
        parsed_entries.append(
            Consultation(
                external_id=_external_id(uri),
                source="hoeringsportalen",
                title=title,
                url=alternate,
                updated_at=updated_at,
                author=author,
                content_text=_content_text(entry.find(f"{ATOM}content")),
                feed_codes=(feed_code,),
                updated_at_raw=updated_raw,
            )
        )
    return ParseResult(tuple(parsed_entries), len(root.findall(f"{ATOM}entry")), invalid_entries, invalid_timestamps)


def fetch_feed(
    feed: FeedConfig,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    opener: Callable[..., object] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Fetch a feed over HTTPS with bounded retries for transient failures."""
    if urlparse(feed.url).scheme.lower() != "https":
        raise FeedFetchError(f"Feed {feed.code} URL must use HTTPS")
    open_request = opener or build_opener(_HTTPSRedirectHandler()).open
    request = Request(feed.url, headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml, application/xml"})
    total_attempts = max(1, retries + 1)
    last_error = "request failed"

    for attempt in range(1, total_attempts + 1):
        try:
            response = open_request(request, timeout=timeout)
            with response as result:  # type: ignore[attr-defined]
                status = result.getcode()
                final_url = result.geturl()
                if status != 200:
                    last_error = f"HTTP {status}"
                    retryable = status == 429 or 500 <= status <= 599
                elif urlparse(final_url).scheme.lower() != "https":
                    raise FeedFetchError(f"Feed {feed.code} redirected to a non-HTTPS URL")
                else:
                    data = result.read()
                    if not data:
                        raise EmptyResponseError(f"Feed {feed.code} returned an empty response")
                    return data
        except HTTPError as exc:
            status = exc.code
            last_error = f"HTTP {status}"
            retryable = status == 429 or 500 <= status <= 599
        except (URLError, TimeoutError, ConnectionError, OSError, HTTPException) as exc:
            last_error = type(exc).__name__
            retryable = True
        except FeedFetchError:
            raise

        if attempt >= total_attempts or not retryable:
            break
        LOGGER.warning("Feed %s attempt %d/%d failed (%s); retrying", feed.code, attempt, total_attempts, last_error)
        sleep(min(0.25 * (2 ** (attempt - 1)), 1.0))

    raise FeedFetchError(last_error)


def _entry_rank(entry: Consultation) -> tuple[object, ...]:
    return (
        _timestamp_key(entry.updated_at),
        sum(value is not None for value in (entry.title, entry.url, entry.author, entry.content_text)),
        entry.title or "",
        entry.url or "",
        entry.author or "",
        entry.content_text or "",
    )


def deduplicate(entries: list[Consultation] | tuple[Consultation, ...]) -> list[Consultation]:
    """Merge duplicate stable IDs, retaining feed membership and newest metadata."""
    grouped: dict[str, Consultation] = {}
    for entry in entries:
        current = grouped.get(entry.external_id)
        if current is None:
            grouped[entry.external_id] = entry
            continue

        feeds = tuple(sorted(set(current.feed_codes) | set(entry.feed_codes)))
        selected = max((current, entry), key=_entry_rank)
        grouped[entry.external_id] = Consultation(
            external_id=selected.external_id,
            source=selected.source,
            title=selected.title,
            url=selected.url,
            updated_at=selected.updated_at,
            author=selected.author,
            content_text=selected.content_text,
            feed_codes=feeds,
            updated_at_raw=selected.updated_at_raw,
        )
        LOGGER.info("Duplicate consultation %s merged from feeds %s", entry.external_id, ",".join(feeds))

    by_id = sorted(grouped.values(), key=lambda item: item.external_id)
    return sorted(by_id, key=lambda item: _timestamp_key(item.updated_at), reverse=True)


def consultation_json(entry: Consultation) -> dict[str, object]:
    """Return the public JSON representation of a normalized consultation."""
    return entry.to_dict()
