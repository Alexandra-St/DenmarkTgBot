from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

from collectors.hoeringsportalen import (
    EmptyResponseError,
    FeedConfig,
    FeedFetchError,
    FeedParseError,
    _parse_updated,
    deduplicate,
    fetch_feed,
    parse_feed,
)
from collectors.main import collect, main

FIXTURE = Path(__file__).parent / "fixtures" / "hearings.xml"
FEED = FeedConfig("test_feed", "https://hoeringsportalen.dk/test-feed")


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, url: str = FEED.url) -> None:
        self.body = body
        self.status = status
        self.url = url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.url

    def read(self) -> bytes:
        return self.body


def sample_feed() -> bytes:
    return FIXTURE.read_bytes()


class ParseTests(unittest.TestCase):
    def test_parses_atom_namespace_ids_fields_and_utc_timestamps(self) -> None:
        result = parse_feed(sample_feed(), "immigration_ministry")
        self.assertEqual(result.entries_seen, 3)
        self.assertEqual(len(result.entries), 3)
        first = result.entries[0]
        self.assertEqual(first.external_id, "71507")
        self.assertEqual(first.title, "Høring om Ukrainesærloven")
        self.assertEqual(first.url, "https://hoeringsportalen.dk/Hearing/Details/71507")
        self.assertEqual(first.updated_at, "2026-09-17T14:27:44.000000Z")
        self.assertEqual(first.updated_at_raw, "2026-09-17T14:27:44Z")
        self.assertEqual(first.author, "Udlændinge- og Integrationsministeriet")
        self.assertEqual(first.feed_codes, ("immigration_ministry",))
        self.assertEqual(result.entries[1].external_id, "https://example.org/consultation/external-42")
        self.assertEqual(result.entries[1].updated_at, "2026-10-01T14:00:00.000000Z")

    def test_missing_optional_fields_are_null(self) -> None:
        entry = parse_feed(sample_feed(), "f").entries[1]
        self.assertIsNone(entry.url)
        self.assertIsNone(entry.author)
        self.assertIsNone(entry.content_text)

    def test_html_content_becomes_readable_text_without_script_content(self) -> None:
        content = parse_feed(sample_feed(), "f").entries[0].content_text
        self.assertEqual(content, "Forslag om ændringer. Høringsfrist: 1. oktober")
        self.assertNotIn("do not include me", content or "")

    def test_invalid_timestamp_is_null_and_reported(self) -> None:
        with self.assertLogs("collectors.hoeringsportalen", level="WARNING") as logs:
            result = parse_feed(sample_feed(), "f")
        self.assertIsNone(result.entries[2].updated_at)
        self.assertEqual(result.invalid_timestamps, 1)
        self.assertIn("invalid updated timestamp", logs.output[0])

    def test_missing_required_entry_fields_are_reported_and_skipped(self) -> None:
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>Missing ID</title></entry>
          <entry><id>https://example.org/2</id></entry>
        </feed>'''
        with self.assertLogs("collectors.hoeringsportalen", level="WARNING") as logs:
            result = parse_feed(xml, "f")
        self.assertEqual(result.entries, ())
        self.assertEqual(result.invalid_entries, 2)
        self.assertEqual(len(logs.output), 2)

    def test_empty_feed_is_successfully_parsed(self) -> None:
        result = parse_feed(b'<feed xmlns="http://www.w3.org/2005/Atom"/>', "f")
        self.assertEqual(result.entries_seen, 0)
        self.assertEqual(result.entries, ())

    def test_malformed_xml_and_non_atom_document_are_errors(self) -> None:
        with self.assertRaisesRegex(FeedParseError, "invalid XML"):
            parse_feed(b"<feed>", "f")
        with self.assertRaisesRegex(FeedParseError, "not an ATOM feed"):
            parse_feed(b"<html/>", "f")

    def test_timestamp_without_timezone_is_invalid(self) -> None:
        self.assertIsNone(_parse_updated("2026-09-17T14:27:44"))
        self.assertEqual(
            _parse_updated("2026-09-17T16:27:44+02:00"),
            "2026-09-17T14:27:44.000000Z",
        )


class DeduplicationTests(unittest.TestCase):
    def test_duplicate_id_keeps_newest_fields_and_all_feed_codes(self) -> None:
        parsed = parse_feed(sample_feed(), "immigration_ministry")
        older = parsed.entries[0]
        newer = replace(
            older,
            title="Updated title",
            updated_at="2026-09-18T10:00:00.000000Z",
            updated_at_raw="2026-09-18T12:00:00+02:00",
            feed_codes=("immigration_category",),
        )
        results = deduplicate([older, newer, parsed.entries[1]])
        match = next(item for item in results if item.external_id == "71507")
        self.assertEqual(match.title, "Updated title")
        self.assertEqual(match.updated_at_raw, "2026-09-18T12:00:00+02:00")
        self.assertEqual(match.feed_codes, ("immigration_category", "immigration_ministry"))

    def test_results_sort_newest_first_with_stable_id_ties(self) -> None:
        entries = list(parse_feed(sample_feed(), "f").entries)
        results = deduplicate(entries)
        self.assertEqual([entry.external_id for entry in results], [
            "https://example.org/consultation/external-42", "71507", "71508"
        ])
        tied = [replace(entry, updated_at="2026-01-01T00:00:00.000000Z") for entry in entries]
        first_order = [item.external_id for item in deduplicate(tied)]
        second_order = [item.external_id for item in deduplicate(list(reversed(tied)))]
        self.assertEqual(first_order, second_order)


class FetchTests(unittest.TestCase):
    def test_retries_temporary_server_error_with_https_and_user_agent(self) -> None:
        response = FakeResponse(sample_feed())
        opener = Mock(side_effect=[FakeResponse(b"", status=503), response])
        sleep = Mock()
        result = fetch_feed(FEED, timeout=4, opener=opener, sleep=sleep)
        self.assertEqual(result, sample_feed())
        self.assertEqual(opener.call_count, 2)
        request = opener.call_args_list[0].args[0]
        self.assertEqual(request.full_url, FEED.url)
        self.assertEqual(request.get_header("User-agent"), "DenmarkTgBot-HoeringsportalenCollector/1.0")
        self.assertEqual(opener.call_args_list[0].kwargs["timeout"], 4)
        sleep.assert_called_once()

    def test_retries_http_429_and_bounds_retries_for_5xx(self) -> None:
        rate_limit = HTTPError(FEED.url, 429, "rate limited", {}, io.BytesIO(b"private body"))
        opener = Mock(side_effect=[rate_limit, FakeResponse(sample_feed())])
        self.assertEqual(fetch_feed(FEED, opener=opener, sleep=Mock()), sample_feed())
        self.assertEqual(opener.call_count, 2)

        unavailable = Mock(return_value=FakeResponse(b"", status=503))
        with self.assertRaisesRegex(FeedFetchError, "HTTP 503") as raised:
            fetch_feed(FEED, retries=2, opener=unavailable, sleep=Mock())
        self.assertEqual(unavailable.call_count, 3)
        self.assertNotIn("private body", str(raised.exception))

    def test_does_not_retry_permanent_http_error_or_empty_response(self) -> None:
        forbidden = Mock(side_effect=HTTPError(FEED.url, 403, "forbidden", {}, None))
        with self.assertRaisesRegex(FeedFetchError, "HTTP 403"):
            fetch_feed(FEED, opener=forbidden, sleep=Mock())
        forbidden.assert_called_once()

        empty = Mock(return_value=FakeResponse(b""))
        with self.assertRaises(EmptyResponseError):
            fetch_feed(FEED, opener=empty, sleep=Mock())
        empty.assert_called_once()

    def test_network_failure_is_bounded_and_non_https_is_rejected(self) -> None:
        broken = Mock(side_effect=URLError("connection refused"))
        with self.assertRaises(FeedFetchError):
            fetch_feed(FEED, retries=1, opener=broken, sleep=Mock())
        self.assertEqual(broken.call_count, 2)
        timeout = Mock(side_effect=TimeoutError("timed out"))
        with self.assertRaisesRegex(FeedFetchError, "TimeoutError"):
            fetch_feed(FEED, retries=0, opener=timeout, sleep=Mock())
        timeout.assert_called_once()
        with self.assertRaisesRegex(FeedFetchError, "HTTPS"):
            fetch_feed(FeedConfig("unsafe", "http://example.org/feed"), opener=Mock())


class CollectionTests(unittest.TestCase):
    def test_one_failed_feed_does_not_discard_successful_feed_data(self) -> None:
        feeds = (FeedConfig("ok", FEED.url), FeedConfig("failed", FEED.url))

        def fetcher(feed: FeedConfig) -> bytes:
            if feed.code == "failed":
                raise FeedFetchError("HTTP 503")
            return sample_feed()

        with self.assertLogs("collectors.main", level="ERROR"):
            output, has_failures = collect(feeds, fetcher, evidence_fetcher=Mock(side_effect=RuntimeError("offline fixture")))
        self.assertTrue(has_failures)
        self.assertEqual(output["feeds"]["ok"]["status"], "success")  # type: ignore[index]
        self.assertEqual(output["feeds"]["failed"]["status"], "unavailable")  # type: ignore[index]
        self.assertEqual(len(output["publications"]), 3)  # type: ignore[arg-type]

    def test_cli_writes_only_json_to_stdout_and_diagnostics_to_stderr(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(fetcher=lambda _: b'<feed xmlns="http://www.w3.org/2005/Atom"/>')
        result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["feeds"]["immigration_ministry"]["status"], "success_empty")
        self.assertEqual(result["publications"], [])
        self.assertIn("INFO: Feed", stderr.getvalue())
        self.assertNotIn("INFO:", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
