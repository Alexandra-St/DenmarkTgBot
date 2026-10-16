from __future__ import annotations

import unittest
from unittest.mock import patch

from collectors.folketinget import Cursor, ODataClient, ODataPageError, ODataPaginationError, ScanMetadata, ScanResult
from collectors.folketinget_sync import _affected_cases, collect, parse_checkpoints

STAMP = "2026-10-14T08:46:08.6566667"


class FakeClient:
    def __init__(self, failed: bool = False):
        self.failed = failed
        self.calls = 0

    def scan(self, collection, *, filter=None, max_pages=None, **kwargs):
        self.calls += 1
        if self.failed and collection == "Sag" and self.calls == 1:
            metadata = ScanMetadata(collection, 0, 0, None, None, False, 1)
            raise ODataPageError(1, ScanResult((), metadata), "cursor did not advance", ODataPaginationError("ODA page cursor did not advance"))
        rows = {"Sag": ({"id": 10, "opdateringsdato": STAMP},)}.get(collection, ())
        upper = Cursor(STAMP, 10) if rows else None
        metadata = ScanMetadata(collection, 1 if rows else 0, len(rows), upper, upper, True, 2 if rows else 1)
        return ScanResult(rows, metadata)


class SyncTests(unittest.TestCase):
    def test_changed_entities_resolve_to_affected_case_ids_by_official_foreign_keys(self):
        changes = {
            "Sag": ({"id": 1},),
            "Sagstrin": ({"id": 2, "sagid": 3},),
            "SagDokument": ({"id": 4, "sagid": 5},),
            "Dokument": ({"id": 6},),
            "Afstemning": ({"id": 7, "sagstrinid": 8}, {"id": 9, "sagstrinid": None}),
            "Sagsstatus": ({"id": 10},),
        }
        refs = [
            ([{"sagid": 11}], True),  # documents -> SagDokument
            ([{"sagid": 12}], True),  # votes -> Sagstrin
            ([{"id": 13}], True),     # status -> Sag
        ]
        with patch("collectors.folketinget_sync._collect_refs", side_effect=refs):
            cases, diagnostics, complete = _affected_cases(object(), changes)
        self.assertEqual(cases, {1, 3, 5, 11, 12, 13})
        self.assertTrue(complete)
        self.assertEqual(diagnostics[0]["code"], "unresolved_vote")

    def test_checkpoint_validation_rejects_bad_values(self):
        self.assertEqual(parse_checkpoints({"Sag": {"timestamp": STAMP, "id": 10}})["Sag"], Cursor(STAMP, 10))
        with self.assertRaises(ValueError):
            parse_checkpoints({"Sag": {"timestamp": "yesterday", "id": 10}})
        with self.assertRaises(ValueError):
            parse_checkpoints({"Unknown": {"timestamp": STAMP, "id": 10}})

    def test_historical_collection_marks_cases_historical_and_never_notify_eligible(self):
        client = FakeClient()
        hydration = type("H", (), {"case": type("C", (), {"to_dict": lambda self: {"external_id": "10", "complete": True}})(), "diagnostics": (), "complete": True})()
        with patch("collectors.folketinget_sync.load_case", return_value=hydration):
            result = collect(client, mode="historical", max_pages=2)
        self.assertTrue(result["historical"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["cases"][0]["historical"], True)
        self.assertEqual(result["cases"][0]["notify_eligible"], False)
        self.assertEqual(set(result["streams"]), {"Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus"})

    def test_cursor_precision_retry_restarts_stream_and_only_then_offers_checkpoint(self):
        client = FakeClient(failed=True)
        with patch("collectors.folketinget_sync.load_case", return_value=type("H", (), {"case": None, "diagnostics": (), "complete": True})()):
            result = collect(client, mode="historical", max_pages=2, precision_retries=1)
        self.assertTrue(result["streams"]["Sag"]["complete"])
        self.assertIn("Sag", result["candidate_checkpoints"])

    def test_incomplete_stream_never_offers_candidate_checkpoint(self):
        class Broken(FakeClient):
            def scan(self, collection, **kwargs):
                if collection == "Sag":
                    metadata = ScanMetadata(collection, 0, 0, None, None, False, 1)
                    raise ODataPageError(1, ScanResult((), metadata), "failed")
                return super().scan(collection, **kwargs)
        result = collect(Broken(), mode="historical", max_pages=2)
        self.assertFalse(result["streams"]["Sag"]["complete"])
        self.assertNotIn("Sag", result["candidate_checkpoints"])


if __name__ == "__main__":
    unittest.main()
