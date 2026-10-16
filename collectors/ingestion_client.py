"""Explicit local uploader for saved collector output; never fetches sources itself."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from collectors.relevance import RULESET_VERSION, classify

MAX_BATCH_BYTES = 220 * 1024
MAX_BATCH_RECORDS = 50
USER_AGENT = "DenmarkTgBot-IngestionClient/1.0"
ODA_STREAMS = ("Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus")
HEARING_FEEDS = ("immigration_ministry", "immigration_category")
_UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,7})?(?:Z|[+-]\d\d:\d\d)$")
_ODA = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,7})?$")
Opener = Callable[..., Any]


class IngestionError(ValueError):
    pass


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
        return [dict(item) for item in value]
    return []


def _utc(value: object) -> str | None:
    return value if isinstance(value, str) and _UTC.fullmatch(value) else None


def _normal_relationships(case: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    documents = []
    for item in _rows(case.get("documents")):
        documents.append({
            "id": item.get("id"), "link_id": item.get("link_id"), "title": item.get("title"),
            "date_raw": item.get("date_raw"), "released_at_raw": item.get("released_at_raw"),
            "document_released_at_raw": item.get("document_released_at_raw"),
            "source_updated_at_raw": item.get("source_updated_at_raw"), "updated_at_utc": item.get("updated_at_utc"),
            "link_source_updated_at_raw": item.get("link_source_updated_at_raw"), "link_updated_at_utc": item.get("link_updated_at_utc"),
        })
    stages = [{
        "id": item.get("id"), "title": item.get("title"), "date_raw": item.get("date_raw"),
        "type_id": item.get("type_id"), "status_id": item.get("status_id"),
        "source_updated_at_raw": item.get("source_updated_at_raw"), "updated_at_utc": item.get("updated_at_utc"),
    } for item in _rows(case.get("stages"))]
    votes = [{
        "id": item.get("id"), "stage_id": item.get("stage_id"), "number": item.get("number"),
        "approved": item.get("approved"), "conclusion": item.get("conclusion"), "comment": item.get("comment"),
        "source_updated_at_raw": item.get("source_updated_at_raw"), "updated_at_utc": item.get("updated_at_utc"),
    } for item in _rows(case.get("votes"))]
    return {"documents": documents, "stages": stages, "votes": votes}


def prepare_records(value: object, source: str, mode: str, run_diagnostics: object = None) -> list[dict[str, Any]]:
    if mode not in {"historical", "incremental"}:
        raise IngestionError("mode must be historical or incremental")
    if source == "folketinget":
        rows = _rows(_dict(value).get("cases"))
    elif source == "hoeringsportalen":
        rows = _rows(_dict(value).get("publications"))
    else:
        raise IngestionError("unsupported source")
    historical = mode == "historical"
    global_diagnostics = _rows(run_diagnostics)
    prepared = []
    for row in rows:
        classified = classify(row).to_dict()
        external_id = str(classified.get("external_id") or "")
        title = row.get("title")
        if not re.fullmatch(r"\d{1,20}", external_id) or not isinstance(title, str) or not title.strip():
            raise IngestionError("record has no valid official numeric ID or title")
        is_complete = row.get("complete", True) is True
        diagnostics = [item for item in global_diagnostics if item.get("entity_id") == int(external_id) or item.get("related_id") == int(external_id)]
        if source == "hoeringsportalen":
            updated = _utc(row.get("updated_at"))
            url = row.get("url") or "https://hoeringsportalen.dk/Hearing/Details/" + external_id
            content = row.get("content_text")
            feed_codes = row.get("feed_codes")
            if not isinstance(feed_codes, list) or not all(code in HEARING_FEEDS for code in feed_codes):
                raise IngestionError("Høringsportalen record has invalid feed membership")
            metadata = {key: row.get(key) for key in ("author", "feed_codes")}
            metadata["updated_at_raw"] = row.get("updated_at_raw", row.get("updated_at"))
            metadata["content_text"] = content
            raw_updated_value = row.get("updated_at_raw", row.get("updated_at"))
            raw_updated = raw_updated_value if isinstance(raw_updated_value, str) else None
            snapshot = {"title": title, "content_text": content, "documents": [], "stages": [], "votes": []}
            publication_type = None
            status = None
        else:
            updated = _utc(row.get("updated_at_utc"))
            url = row.get("source_url")
            status_obj = _dict(row.get("status"))
            status = status_obj.get("name") if isinstance(status_obj.get("name"), str) else None
            publication_type = row.get("number") if isinstance(row.get("number"), str) else None
            raw_updated = row.get("source_updated_at_raw") if isinstance(row.get("source_updated_at_raw"), str) else None
            related = _normal_relationships(row)
            snapshot = {"title": title, "content_text": None, "status": status, "publication_type": publication_type, **related}
            metadata = {
                "number": row.get("number"), "type_id": row.get("type_id"), "category_id": row.get("category_id"),
                "status_id": status_obj.get("id"), "source_updated_at_raw": raw_updated,
                "updated_at_utc": updated, "legal_url": row.get("legal_url"),
            }
            if not isinstance(url, str):
                raise IngestionError("Folketinget case has no official source URL")
        if not isinstance(url, str):
            raise IngestionError("publication has no source URL")
        prepared.append({
            "source": source, "external_id": external_id, "title": title, "url": url,
            "relevance": classified["relevance"], "relevance_reason": classified["reason"],
            "ruleset_version": RULESET_VERSION, "status": status, "publication_type": publication_type,
            "published_at": None, "source_updated_at": updated, "source_updated_at_raw": raw_updated,
            "historical": historical, "notification_blocked": bool(classified.get("notification_blocked")) or not is_complete,
            "complete": is_complete, "diagnostics": [*classified.get("diagnostics", []), *diagnostics],
            "source_metadata": metadata, "normalized_snapshot": snapshot,
        })
    return prepared


def collection_complete(value: object, source: str) -> bool:
    root = _dict(value)
    if source == "folketinget":
        streams = _dict(root.get("streams"))
        return root.get("complete") is True and set(streams) == set(ODA_STREAMS) and all(
            _dict(streams.get(name)).get("complete") is True for name in ODA_STREAMS
        )
    feeds = _dict(root.get("feeds"))
    if not set(HEARING_FEEDS).issubset(feeds):
        return False
    for name in HEARING_FEEDS:
        feed = _dict(feeds.get(name))
        if feed.get("status") not in {"success", "success_empty"}:
            return False
        if any(feed.get(key, 0) != 0 for key in ("invalid_entries", "invalid_timestamps", "missing_updated_at", "invalid_cursor_ids")):
            return False
    for row in _rows(root.get("publications")):
        if not _utc(row.get("updated_at")):
            return False
    return True


def _cursor(value: object, source: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise IngestionError("invalid saved checkpoint")
    timestamp = value.get("timestamp")
    entity_id = value.get("id")
    valid = _UTC.fullmatch(timestamp) if source == "hoeringsportalen" and isinstance(timestamp, str) else _ODA.fullmatch(timestamp) if isinstance(timestamp, str) else None
    if not valid or isinstance(entity_id, bool) or not isinstance(entity_id, int) or entity_id < 0:
        raise IngestionError("invalid saved checkpoint")
    return {"timestamp": timestamp, "id": entity_id}


def _cursor_key(value: Mapping[str, Any]) -> tuple[datetime, int]:
    timestamp = datetime.fromisoformat(str(value["timestamp"]).replace("Z", "+00:00"))
    return timestamp.astimezone(timezone.utc), int(value["id"])


def proposed_checkpoints(value: object, source: str, current: object) -> dict[str, dict[str, Any] | None]:
    current_rows = _dict(current)
    saved: dict[str, dict[str, Any] | None] = {}
    for item in _rows(current_rows.get("streams")):
        name = item.get("stream")
        if isinstance(name, str):
            saved[name] = _cursor(item.get("checkpoint"), source)
    keys = ODA_STREAMS if source == "folketinget" else HEARING_FEEDS
    if source == "folketinget":
        root = _dict(value)
        stream_rows = _dict(root.get("streams"))
        proposed = {}
        for name in keys:
            upper = _dict(stream_rows.get(name)).get("upper_bound")
            candidate = _cursor(upper, source)
            previous = saved.get(name)
            proposed[name] = candidate or previous
        return proposed
    proposed: dict[str, dict[str, Any] | None] = {}
    feed_rows = _dict(_dict(value).get("feeds"))
    for name in keys:
        candidate = _cursor(_dict(feed_rows.get(name)).get("upper_bound"), source)
        previous = saved.get(name)
        if candidate is None:
            proposed[name] = previous
        elif previous is None or _cursor_key(candidate) >= _cursor_key(previous):
            proposed[name] = candidate
        else:
            proposed[name] = previous
    return proposed


def filter_incremental_hearings(records: list[dict[str, Any]], current: object) -> list[dict[str, Any]]:
    """Keep records ahead of at least one independent feed checkpoint.

    An unset feed checkpoint intentionally includes its full current window.
    Equal cursor pairs are considered already observed.
    """
    saved: dict[str, dict[str, Any] | None] = {}
    for item in _rows(_dict(current).get("streams")):
        name = item.get("stream")
        if name in HEARING_FEEDS:
            saved[str(name)] = _cursor(item.get("checkpoint"), "hoeringsportalen")

    selected: list[dict[str, Any]] = []
    for record in records:
        feeds = _dict(record.get("source_metadata")).get("feed_codes")
        if not isinstance(feeds, list) or not feeds:
            selected.append(record)
            continue
        timestamp = record.get("source_updated_at")
        external_id = record.get("external_id")
        if not _utc(timestamp) or not isinstance(external_id, str) or not external_id.isdigit():
            raise IngestionError("publication has an invalid incremental cursor")
        candidate = (datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc), int(external_id))
        if any(
            feed not in HEARING_FEEDS
            or saved.get(feed) is None
            or candidate > _cursor_key(saved[feed])
            for feed in feeds
        ):
            selected.append(record)
    return selected


def chunks(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for record in records:
        candidate = current + [record]
        encoded = json.dumps({"records": candidate}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(candidate) > MAX_BATCH_RECORDS or len(encoded) > MAX_BATCH_BYTES:
            if not current:
                raise IngestionError("one record exceeds the safe request size")
            batches.append(current)
            current = [record]
            encoded = json.dumps({"records": current}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > MAX_BATCH_BYTES:
                raise IngestionError("one record exceeds the safe request size")
        else:
            current = candidate
    if current or not batches:
        batches.append(current)
    return batches


def _request(endpoint: str, token: str, method: str, payload: object | None, opener: Opener = urllib.request.urlopen) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=body, method=method,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": USER_AGENT, **({"Content-Type": "application/json"} if body is not None else {})},
    )
    for attempt in range(3):
        try:
            with opener(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
                if not isinstance(result, dict):
                    raise IngestionError("server returned an invalid response")
                return result
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or 500 <= exc.code <= 599:
                if attempt < 2:
                    time.sleep(0.2 * (2 ** attempt))
                    continue
            try:
                error = json.loads(exc.read().decode("utf-8")).get("error", "server_error")
            except Exception:
                error = "server_error"
            raise IngestionError("server rejected request: " + str(error)) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt < 2:
                time.sleep(0.2 * (2 ** attempt))
                continue
            raise IngestionError("ingestion service is unavailable") from None
    raise IngestionError("ingestion service is unavailable")


def upload(value: object, *, source: str, mode: str, endpoint: str, token: str, dry_run: bool = False, run_id: str | None = None, opener: Opener = urllib.request.urlopen) -> dict[str, Any]:
    complete = collection_complete(value, source)
    records = prepare_records(value, source, mode, _dict(value).get("diagnostics"))
    batches = chunks(records)
    run_id = run_id or str(uuid.uuid4())
    counts: dict[str, int] = {}
    for item in records:
        counts[item["relevance"]] = counts.get(item["relevance"], 0) + 1
    if dry_run:
        return {"dry_run": True, "source": source, "mode": mode, "run_id": run_id, "collection_complete": complete, "records": len(records), "batches": len(batches), "relevance_counts": counts, "would_submit": complete}
    if not complete:
        raise IngestionError("collector output is incomplete; no records or checkpoints were submitted")
    if not endpoint.startswith("https://") or not token:
        raise IngestionError("set INGEST_API_URL and INGEST_API_TOKEN, or use --dry-run")
    state_url = endpoint.rstrip("/") + "/internal/source-states?source=" + source
    state = _request(state_url, token, "GET", None, opener)
    if source == "hoeringsportalen" and mode == "incremental":
        records = filter_incremental_hearings(records, state)
        batches = chunks(records)
        counts = {}
        for item in records:
            counts[item["relevance"]] = counts.get(item["relevance"], 0) + 1
    checkpoints = proposed_checkpoints(value, source, state)
    _request(endpoint.rstrip("/") + "/internal/ingest", token, "POST", {
        "action": "start", "run_id": run_id, "source": source, "mode": mode,
        "expected_batches": len(batches), "collection_complete": True, "proposed_checkpoints": checkpoints,
    }, opener)
    accepted = 0
    try:
        for index, batch in enumerate(batches):
            result = _request(endpoint.rstrip("/") + "/internal/ingest", token, "POST", {
                "action": "batch", "run_id": run_id, "batch_index": index, "records": batch,
            }, opener)
            accepted += int(result.get("accepted_records", 0))
    except IngestionError:
        raise IngestionError("batch submission failed; retry with --run-id " + run_id + " to resume, or abort this open run") from None
    while True:
        result = _request(endpoint.rstrip("/") + "/internal/ingest", token, "POST", {"action": "commit", "run_id": run_id}, opener)
        if result.get("status") in {"COMMITTED", "BLOCKED"}:
            return {"run_id": run_id, "status": result["status"], "accepted_records": accepted, "processed_records": result.get("processed_records", 0), "checkpoints_advanced": result.get("checkpoints_advanced", False), "relevance_counts": counts}
        if result.get("status") != "COMMITTING":
            raise IngestionError("server returned an unknown run state; retry using --run-id " + run_id)


def main(argv: list[str] | None = None, opener: Opener = urllib.request.urlopen) -> int:
    parser = argparse.ArgumentParser(description="Classify and upload saved collector JSON; never fetches source websites.")
    parser.add_argument("input", type=Path)
    parser.add_argument("--source", choices=("folketinget", "hoeringsportalen"))
    parser.add_argument("--mode", choices=("historical", "incremental"), default="incremental")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-id", help="Reuse the same UUID to resume a partially submitted run")
    parser.add_argument("--abort-open-run", help="Abort a run that has not started commit processing")
    args = parser.parse_args(argv)
    try:
        value = json.loads(args.input.read_text(encoding="utf-8"))
        source = args.source or ("folketinget" if isinstance(value, dict) and "cases" in value else "hoeringsportalen")
        if args.abort_open_run:
            endpoint = os.environ.get("INGEST_API_URL", "").rstrip("/") + "/internal/ingest"
            token = os.environ.get("INGEST_API_TOKEN", "")
            if not endpoint.startswith("https://") or not token:
                raise IngestionError("set INGEST_API_URL and INGEST_API_TOKEN")
            result = _request(endpoint, token, "POST", {"action": "abort", "run_id": args.abort_open_run}, opener)
        else:
            result = upload(value, source=source, mode=args.mode, endpoint=os.environ.get("INGEST_API_URL", ""), token=os.environ.get("INGEST_API_TOKEN", ""), dry_run=args.dry_run, run_id=args.run_id, opener=opener)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result.get("status") != "BLOCKED" and result.get("would_submit", True) else 2
    except (OSError, json.JSONDecodeError, IngestionError) as exc:
        print("ingestion client: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
