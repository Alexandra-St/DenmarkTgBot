"""Single-run command-line entry point for Høringsportalen feeds."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence

from collectors.hoeringsportalen import (
    FEEDS,
    FeedConfig,
    FeedFetchError,
    FeedParseError,
    Consultation,
    deduplicate,
    fetch_feed,
    parse_feed,
)

LOGGER = logging.getLogger(__name__)
Fetcher = Callable[[FeedConfig], bytes]


def collect(
    feeds: Sequence[FeedConfig] = FEEDS,
    fetcher: Fetcher | None = None,
) -> tuple[dict[str, object], bool]:
    """Fetch all configured feeds and return JSON-ready partial results."""
    retrieve = fetcher or fetch_feed
    all_entries: list[Consultation] = []
    feed_report: dict[str, dict[str, object]] = {}
    any_failed = False

    for feed in feeds:
        try:
            xml_data = retrieve(feed)
        except FeedFetchError as exc:
            LOGGER.error("Feed %s unavailable: %s", feed.code, exc)
            feed_report[feed.code] = {"status": "unavailable", "error": str(exc)}
            any_failed = True
            continue

        try:
            parsed = parse_feed(xml_data, feed.code)
        except FeedParseError as exc:
            LOGGER.error("Feed %s invalid: %s", feed.code, exc)
            feed_report[feed.code] = {"status": "invalid_xml", "error": str(exc)}
            any_failed = True
            continue

        all_entries.extend(parsed.entries)
        status = "success_empty" if parsed.entries_seen == 0 else "success"
        upper = None
        missing_updates = 0
        invalid_cursor_ids = 0
        for entry in parsed.entries:
            if entry.updated_at is None:
                missing_updates += 1
                continue
            if not entry.external_id.isdigit():
                invalid_cursor_ids += 1
                continue
            candidate = {"timestamp": entry.updated_at, "id": int(entry.external_id)}
            if upper is None or (candidate["timestamp"], candidate["id"]) > (upper["timestamp"], upper["id"]):
                upper = candidate
        feed_report[feed.code] = {
            "status": status,
            "entries_seen": parsed.entries_seen,
            "entries_parsed": len(parsed.entries),
            "invalid_entries": parsed.invalid_entries,
            "invalid_timestamps": parsed.invalid_timestamps,
            "missing_updated_at": missing_updates,
            "invalid_cursor_ids": invalid_cursor_ids,
            "upper_bound": upper,
        }
        if parsed.entries_seen == 0:
            LOGGER.info("Feed %s returned no entries", feed.code)
        else:
            LOGGER.info("Feed %s parsed %d/%d entries", feed.code, len(parsed.entries), parsed.entries_seen)

    publications = deduplicate(all_entries)
    feed_report["summary"] = {
        "successful_feeds": sum(item.get("status", "").startswith("success") for item in feed_report.values()),
        "failed_feeds": sum(item.get("status") in {"unavailable", "invalid_xml"} for item in feed_report.values()),
        "unique_publications": len(publications),
        "duplicate_entries_merged": len(all_entries) - len(publications),
    }
    result: dict[str, object] = {
        "publications": [item.to_dict() for item in publications],
        "feeds": feed_report,
    }
    return result, any_failed


def main(argv: Sequence[str] | None = None, fetcher: Fetcher | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and normalize Høringsportalen ATOM feeds.")
    parser.parse_args(argv if argv is not None else ())
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
    result, has_failures = collect(fetcher=fetcher)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 1 if has_failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
