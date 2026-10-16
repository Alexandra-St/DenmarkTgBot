from __future__ import annotations

import json
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError

from collectors.ingestion_client import (
    IngestionError,
    USER_AGENT,
    chunks,
    collection_complete,
    prepare_records,
    proposed_checkpoints,
    upload,
)

STAMP = "2026-10-16T10:11:12.1234567"


def hearing_output() -> dict[str, object]:
    return {
        "feeds": {
            "immigration_ministry": {
                "status": "success", "invalid_entries": 0, "invalid_timestamps": 0,
                "missing_updated_at": 0, "invalid_cursor_ids": 0,
                "upper_bound": {"timestamp": "2026-10-16T10:11:12.123456Z", "id": 71507},
            },
            "immigration_category": {
                "status": "success_empty", "invalid_entries": 0, "invalid_timestamps": 0,
                "missing_updated_at": 0, "invalid_cursor_ids": 0, "upper_bound": None,
            },
        },
        "publications": [{
            "source": "hoeringsportalen", "external_id": "71507", "title": "Ukrainesærloven",
            "url": "https://hoeringsportalen.dk/Hearing/Details/71507",
            "updated_at": "2026-10-16T10:11:12.123456Z", "author": "Ministry",
            "content_text": "Proposed changes", "feed_codes": ["immigration_ministry", "immigration_category"],
        }],
    }


def oda_output() -> dict[str, object]:
    stream_names = ("Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus")
    return {
        "complete": True,
        "diagnostics": [],
        "streams": {name: {"complete": True, "upper_bound": {"timestamp": STAMP, "id": index + 1}} for index, name in enumerate(stream_names)},
        "cases": [{
            "source": "folketinget", "external_id": "42", "number": "L 1",
            "title": "Forslag til Ukrainesærloven", "type_id": 3, "category_id": 4,
            "status": {"id": 2, "name": "Fremsat"}, "source_updated_at_raw": STAMP,
            "updated_at_utc": None, "source_url": "https://oda.ft.dk/api/Sag(42)",
            "legal_url": None, "stages": [{"id": 7, "title": "Fremsat", "date_raw": "2026-10-01", "type_id": 1, "status_id": 2, "source_updated_at_raw": STAMP, "updated_at_utc": None}],
            "documents": [{"id": 11, "link_id": 12, "title": "Lovudkast", "date_raw": None, "released_at_raw": None, "document_released_at_raw": None, "source_updated_at_raw": STAMP, "updated_at_utc": None, "link_source_updated_at_raw": STAMP, "link_updated_at_utc": None}],
            "votes": [{"id": 9, "stage_id": 7, "number": 1, "approved": True, "conclusion": "Vedtaget", "comment": None, "source_updated_at_raw": STAMP, "updated_at_utc": None}],
            "complete": True,
        }],
    }


class FakeHTTPResponse:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.value).encode()


class ClientTests(unittest.TestCase):
    def test_prepares_historical_hearing_without_live_fetch(self):
        value = hearing_output()
        value["publications"][0]["updated_at_raw"] = "2026-10-16T10:11:12.123456+00:00"
        result = prepare_records(value, "hoeringsportalen", "historical")[0]
        self.assertTrue(result["historical"])
        self.assertEqual(result["source_updated_at"], "2026-10-16T10:11:12.123456Z")
        self.assertEqual(result["source_metadata"]["feed_codes"], ["immigration_ministry", "immigration_category"])
        self.assertEqual(result["source_updated_at_raw"], "2026-10-16T10:11:12.123456+00:00")
        self.assertEqual(result["normalized_snapshot"]["content_text"], "Proposed changes")
        self.assertEqual(result["ruleset_version"], "1.1")

    def test_oda_raw_offset_free_update_is_preserved_but_not_converted_to_utc(self):
        row = prepare_records(oda_output(), "folketinget", "incremental")[0]
        self.assertIsNone(row["source_updated_at"])
        self.assertEqual(row["source_updated_at_raw"], STAMP)
        self.assertEqual(row["source_metadata"]["source_updated_at_raw"], STAMP)
        self.assertEqual(row["normalized_snapshot"]["documents"][0]["id"], 11)
        self.assertEqual(row["normalized_snapshot"]["stages"][0]["id"], 7)
        self.assertEqual(row["normalized_snapshot"]["votes"][0]["id"], 9)

    def test_collection_must_be_complete_before_any_submission(self):
        value = hearing_output()
        value["feeds"]["immigration_category"]["invalid_timestamps"] = 1
        self.assertFalse(collection_complete(value, "hoeringsportalen"))
        opener = Mock(side_effect=AssertionError("no HTTP request expected"))
        with self.assertRaisesRegex(IngestionError, "incomplete"):
            upload(value, source="hoeringsportalen", mode="incremental", endpoint="https://example.invalid", token="test-only", opener=opener)
        opener.assert_not_called()

    def test_feed_checkpoints_are_independent_and_empty_feed_keeps_saved_cursor(self):
        current = {"streams": [
            {"stream": "immigration_ministry", "checkpoint": {"timestamp": "2026-10-15T00:00:00Z", "id": 1}},
            {"stream": "immigration_category", "checkpoint": {"timestamp": "2026-10-14T00:00:00Z", "id": 2}},
        ]}
        result = proposed_checkpoints(hearing_output(), "hoeringsportalen", current)
        self.assertEqual(result["immigration_ministry"], {"timestamp": "2026-10-16T10:11:12.123456Z", "id": 71507})
        self.assertEqual(result["immigration_category"], {"timestamp": "2026-10-14T00:00:00Z", "id": 2})

    def test_oda_checkpoints_preserve_fractional_precision_and_prior_empty_stream(self):
        value = oda_output()
        value["streams"]["Afstemning"]["upper_bound"] = None
        current = {"streams": [{"stream": "Afstemning", "checkpoint": {"timestamp": STAMP, "id": 55}}]}
        result = proposed_checkpoints(value, "folketinget", current)
        self.assertEqual(result["Sag"], {"timestamp": STAMP, "id": 1})
        self.assertEqual(result["Afstemning"], {"timestamp": STAMP, "id": 55})

    def test_upload_uses_authenticated_mocked_http_and_sends_bounded_idempotent_batches(self):
        value = hearing_output()
        responses = [
            {"source": "hoeringsportalen", "streams": [{"stream": "immigration_ministry", "checkpoint": None}, {"stream": "immigration_category", "checkpoint": None}]},
            {"accepted": True, "status": "OPEN"},
            {"accepted": True, "accepted_records": 1},
            {"run_id": "run", "status": "COMMITTED", "processed_records": 1, "checkpoints_advanced": True},
        ]
        requests = []

        def opener(request, timeout):
            requests.append(request)
            return FakeHTTPResponse(responses.pop(0))

        result = upload(value, source="hoeringsportalen", mode="incremental", endpoint="https://ingest.example", token="test-only-token", run_id="run", opener=opener)
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(result["accepted_records"], 1)
        self.assertEqual(len(requests), 4)
        self.assertTrue(all(request.get_header("Authorization") == "Bearer test-only-token" for request in requests))
        self.assertTrue(all(request.get_header("User-agent") == USER_AGENT for request in requests))
        start = json.loads(requests[1].data)
        self.assertEqual(start["proposed_checkpoints"]["immigration_ministry"]["id"], 71507)
        self.assertEqual(start["proposed_checkpoints"]["immigration_category"], None)
        self.assertEqual(json.loads(requests[2].data)["records"][0]["source"], "hoeringsportalen")

    def test_blocked_commit_is_reported_as_non_success(self):
        responses = [
            {"source": "hoeringsportalen", "streams": []},
            {"accepted": True, "status": "OPEN"},
            {"accepted": True, "accepted_records": 1},
            {"run_id": "run", "status": "BLOCKED", "processed_records": 0, "checkpoints_advanced": False},
        ]

        def opener(request, timeout):
            return FakeHTTPResponse(responses.pop(0))

        result = upload(hearing_output(), source="hoeringsportalen", mode="incremental", endpoint="https://ingest.example", token="test-only-token", run_id="run", opener=opener)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["checkpoints_advanced"])

    def test_upload_reconciles_all_current_feed_rows_even_when_checkpoints_are_unchanged(self):
        value = hearing_output()
        checkpoint = {"timestamp": "2026-10-16T10:11:12.123456Z", "id": 71507}
        responses = [
            {"source": "hoeringsportalen", "streams": [
                {"stream": "immigration_ministry", "checkpoint": checkpoint},
                {"stream": "immigration_category", "checkpoint": checkpoint},
            ]},
            {"accepted": True, "status": "OPEN"},
            {"accepted": True, "accepted_records": 1},
            {"run_id": "run", "status": "COMMITTED", "processed_records": 1, "checkpoints_advanced": True},
        ]
        requests = []

        def opener(request, timeout):
            requests.append(request)
            return FakeHTTPResponse(responses.pop(0))

        result = upload(value, source="hoeringsportalen", mode="incremental", endpoint="https://ingest.example", token="test-only-token", run_id="run", opener=opener)
        self.assertEqual(result["accepted_records"], 1)
        sent = json.loads(requests[2].data)["records"]
        self.assertEqual([record["external_id"] for record in sent], ["71507"])
        self.assertEqual(sent[0]["source_metadata"]["feed_codes"], ["immigration_ministry", "immigration_category"])

    def test_upload_reconciles_all_current_window_records_behind_and_ahead_of_checkpoint(self):
        value = hearing_output()
        original = value["publications"][0]
        behind = {**original, "external_id": "71506", "title": "Behind cursor", "updated_at": "2020-01-01T00:00:00.000000Z", "feed_codes": ["immigration_ministry"]}
        ahead = {**original, "external_id": "71508", "title": "Ahead of cursor", "updated_at": "2026-10-17T00:00:00.000000Z", "feed_codes": ["immigration_category"]}
        value["publications"].extend([behind, ahead])
        checkpoint = {"timestamp": "2026-10-16T10:11:12.123456Z", "id": 71507}
        responses = [
            {"source": "hoeringsportalen", "streams": [
                {"stream": "immigration_ministry", "checkpoint": checkpoint},
                {"stream": "immigration_category", "checkpoint": checkpoint},
            ]},
            {"accepted": True, "status": "OPEN"},
            {"accepted": True, "accepted_records": 3},
            {"run_id": "run", "status": "COMMITTED", "processed_records": 3, "checkpoints_advanced": True},
        ]
        requests = []

        def opener(request, timeout):
            requests.append(request)
            return FakeHTTPResponse(responses.pop(0))

        result = upload(value, source="hoeringsportalen", mode="incremental", endpoint="https://ingest.example", token="test-only-token", run_id="run", opener=opener)
        self.assertEqual(result["accepted_records"], 3)
        sent = json.loads(requests[2].data)["records"]
        self.assertEqual({record["external_id"] for record in sent}, {"71506", "71507", "71508"})
        start = json.loads(requests[1].data)
        self.assertEqual(start["proposed_checkpoints"], {"immigration_ministry": checkpoint, "immigration_category": checkpoint})

    def test_failed_feed_prevents_all_ingestion_requests(self):
        value = hearing_output()
        value["feeds"]["immigration_category"]["status"] = "unavailable"
        opener = Mock(side_effect=AssertionError("incomplete current window must not submit"))
        with self.assertRaisesRegex(IngestionError, "incomplete"):
            upload(value, source="hoeringsportalen", mode="incremental", endpoint="https://ingest.example", token="test-only-token", opener=opener)
        opener.assert_not_called()

    def test_dry_run_does_not_send_http_and_reports_incomplete_status(self):
        opener = Mock(side_effect=AssertionError("dry run must not perform HTTP"))
        result = upload(hearing_output(), source="hoeringsportalen", mode="incremental", endpoint="", token="", dry_run=True, opener=opener)
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["would_submit"])
        opener.assert_not_called()

    def test_chunking_obeys_both_record_and_request_size_limits(self):
        records = [{"value": "x" * 120_000}, {"value": "y" * 120_000}, {"value": "z"}]
        batches = chunks(records)
        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(map(len, batches)), len(records))
        with self.assertRaisesRegex(IngestionError, "exceeds"):
            chunks([{"value": "x" * 300_000}])


if __name__ == "__main__":
    unittest.main()
