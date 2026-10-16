from __future__ import annotations

import unittest
from threading import Barrier, BrokenBarrierError, Lock
from unittest.mock import patch

from collectors.folketinget import Cursor, ODataClient, ODataPageError, ODataPaginationError, ScanMetadata, ScanResult
from collectors.folketinget_relations import BulkHydrationResult
from collectors.folketinget_sync import _affected_cases, _page_failure_category, _scan, collect, parse_checkpoints

STAMP = "2026-10-14T08:46:08.6566667"


class FakeClient:
    def __init__(self, failed: bool = False):
        self.failed = failed
        self.calls = 0
        self.filters = {}
        self.sag_calls = 0

    def scan(self, collection, *, filter=None, max_pages=None, **kwargs):
        self.calls += 1
        self.filters.setdefault(collection, []).append(filter)
        if collection == "Sag":
            self.sag_calls += 1
        if self.failed and collection == "Sag" and self.sag_calls == 1:
            metadata = ScanMetadata(collection, 0, 0, None, None, False, 1)
            raise ODataPageError(1, ScanResult((), metadata), "cursor did not advance", ODataPaginationError("ODA page cursor did not advance"))
        rows = {"Sag": ({"id": 10, "nummer": "L 10", "titel": "Forslag", "typeid": 1, "kategoriid": 1, "statusid": None, "retsinformationsurl": None, "opdateringsdato": STAMP},)}.get(collection, ())
        upper = Cursor(STAMP, 10) if rows else None
        metadata = ScanMetadata(collection, 1 if rows else 0, len(rows), upper, upper, True, 2 if rows else 1)
        return ScanResult(rows, metadata)


class SyncTests(unittest.TestCase):
    @staticmethod
    def hydration_result(case=None, *, complete=True, diagnostics=()):
        cases = (case,) if case is not None else ()
        return BulkHydrationResult(cases, diagnostics, complete)

    def test_changed_entities_resolve_to_affected_case_ids_by_official_foreign_keys(self):
        changes = {
            "Sag": ({"id": 1},),
            "Sagstrin": ({"id": 2, "sagid": 3},),
            "SagDokument": ({"id": 4, "sagid": 5},),
            "Dokument": ({"id": 6},),
            "Afstemning": ({"id": 7, "sagstrinid": 8}, {"id": 9, "sagstrinid": None}),
            "Sagsstatus": ({"id": 10},),
        }
        refs = {
            "SagDokument": ([{"sagid": 11}], True),
            "Sagstrin": ([{"sagid": 12}], True),
            "Sag": ([{"id": 13}], True),
        }
        with patch("collectors.folketinget_sync._collect_refs", side_effect=lambda _client, entity, *_args: refs[entity]):
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

    def test_six_stream_scans_use_bounded_parallel_workers_and_required_projections(self):
        streams = {"Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus"}
        rendezvous = Barrier(4)
        lock = Lock()
        first_stream_calls = 0
        select_by_stream = {}

        class ParallelClient:
            def scan(self, collection, *, filter=None, select=None, **_kwargs):
                nonlocal first_stream_calls
                is_stream_scan = collection in streams and "opdateringsdato ge datetime'" in (filter or "")
                if is_stream_scan:
                    with lock:
                        first_stream_calls += 1
                        position = first_stream_calls
                        select_by_stream[collection] = select
                    if position <= 4:
                        try:
                            rendezvous.wait(timeout=2)
                        except BrokenBarrierError as exc:
                            raise AssertionError("stream scans did not overlap") from exc
                metadata = ScanMetadata(collection, 0, 0, None, None, True, 1)
                return ScanResult((), metadata)

        with patch("collectors.folketinget_sync.load_cases", return_value=self.hydration_result()):
            result = collect(ParallelClient(), mode="historical", max_workers=4)
        self.assertTrue(result["complete"])
        self.assertEqual(set(select_by_stream), streams)
        self.assertEqual(select_by_stream["Sag"], "id,opdateringsdato,statusid")
        self.assertEqual(select_by_stream["Afstemning"], "id,opdateringsdato,sagstrinid")
        self.assertEqual(set(result["streams"]), streams)

    def test_historical_collection_marks_cases_historical_and_never_notify_eligible(self):
        client = FakeClient()
        case = type("C", (), {"to_dict": lambda self: {"external_id": "10", "complete": True}})()
        with patch("collectors.folketinget_sync.load_cases", return_value=self.hydration_result(case)):
            result = collect(client, mode="historical", max_pages=2)
        self.assertTrue(result["historical"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["cases"][0]["historical"], True)
        self.assertIs(result["cases"][0]["notify_eligible"], False)
        self.assertEqual(set(result["streams"]), {"Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus"})
        self.assertEqual(result["committable_checkpoints"], {})

    def test_incremental_missing_stream_checkpoint_uses_configured_historical_lookback(self):
        client = FakeClient()
        with patch("collectors.folketinget_sync._floor", return_value="LOOKBACK_FLOOR"):
            with patch("collectors.folketinget_sync.load_cases", return_value=self.hydration_result()):
                result = collect(client, mode="incremental", checkpoints={"Sag": Cursor(STAMP, 10)}, days=21, overlap_hours=36)
        for name in {"Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus"}:
            self.assertIn("LOOKBACK_FLOOR", client.filters[name][0])
        self.assertEqual(result["committable_checkpoints"], {})

    def test_incremental_notification_eligibility_is_unknown(self):
        client = FakeClient()
        case = type("C", (), {"to_dict": lambda self: {"external_id": "10", "complete": True}})()
        with patch("collectors.folketinget_sync.load_cases", return_value=self.hydration_result(case)):
            result = collect(client, mode="incremental", checkpoints={})
        self.assertIsNone(result["cases"][0]["notify_eligible"])
        self.assertFalse(result["cases"][0]["historical"])
        self.assertEqual(result["committable_checkpoints"], {})

    def test_cursor_precision_retry_restarts_stream_without_offering_checkpoint(self):
        client = FakeClient(failed=True)
        with patch("collectors.folketinget_sync.load_cases", return_value=self.hydration_result()):
            result = collect(client, mode="historical", max_pages=2, precision_retries=1)
        self.assertTrue(result["streams"]["Sag"]["complete"])
        self.assertEqual(result["committable_checkpoints"], {})
        self.assertIn("precision_retry_recovered", {item["code"] for item in result["diagnostics"]})

    def test_successful_restart_discards_partial_rows_from_precision_conflict(self):
        partial_row = {"id": 1, "opdateringsdato": "2026-10-10T00:00:00"}
        complete_row = {"id": 2, "opdateringsdato": STAMP}
        failed_meta = ScanMetadata("Sag", 1, 1, Cursor(STAMP, 2), Cursor(STAMP, 1), False, 2)
        complete_meta = ScanMetadata("Sag", 1, 1, Cursor(STAMP, 2), Cursor(STAMP, 2), True, 2)

        class Retry:
            calls = 0

            def scan(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise ODataPageError(2, ScanResult((partial_row,), failed_meta), "precision", ODataPaginationError("ODA page exceeded the captured upper bound"))
                return ScanResult((complete_row,), complete_meta)

        scan, rows, status = _scan(Retry(), "Sag", STAMP, 10, 1)
        self.assertEqual(scan.records, (complete_row,))
        self.assertEqual(rows, [complete_row])
        self.assertEqual(status, "complete_after_precision_retry")

    def test_page_failure_diagnostics_distinguish_pagination_reasons_without_ids(self):
        metadata = ScanMetadata("Dokument", 1, 1, Cursor(STAMP, 9), Cursor(STAMP, 8), False, 3)
        partial = ScanResult(({"id": 8, "opdateringsdato": STAMP},), metadata)
        repeated = ODataPageError(2, partial, "unsafe detail omitted", ODataPaginationError("ODA page cursor did not advance"), request_url_chars=653)
        above_bound = ODataPageError(2, partial, "unsafe detail omitted", ODataPaginationError("ODA page exceeded the captured upper bound"), request_url_chars=660)
        self.assertEqual(_page_failure_category(repeated), "incomplete:cursor_not_advanced:page=2:url_chars=653")
        self.assertEqual(_page_failure_category(above_bound), "incomplete:above_captured_upper_bound:page=2:url_chars=660")
        self.assertNotIn("8", _page_failure_category(repeated))

    def test_incomplete_stream_never_offers_candidate_checkpoint(self):
        class Broken(FakeClient):
            def scan(self, collection, **kwargs):
                if collection == "Sag":
                    metadata = ScanMetadata(collection, 0, 0, None, None, False, 1)
                    raise ODataPageError(1, ScanResult((), metadata), "failed")
                return super().scan(collection, **kwargs)
        result = collect(Broken(), mode="historical", max_pages=2)
        self.assertFalse(result["streams"]["Sag"]["complete"])
        self.assertEqual(result["committable_checkpoints"], {})

    def test_relation_or_hydration_failure_keeps_partial_output_without_checkpoints(self):
        client = FakeClient()
        case = type("C", (), {"to_dict": lambda self: {"external_id": "10", "complete": False}})()
        hydration = self.hydration_result(case, complete=False)
        with patch("collectors.folketinget_sync._collect_refs", return_value=([], False)):
            with patch("collectors.folketinget_sync.load_cases", return_value=hydration):
                result = collect(client, mode="historical")
        self.assertFalse(result["complete"])
        self.assertEqual(result["cases"][0]["external_id"], "10")
        self.assertEqual(result["committable_checkpoints"], {})

    def test_null_stage_vote_is_nonblocking_observation(self):
        client = FakeClient()
        original = client.scan

        def scan(collection, **kwargs):
            if collection == "Afstemning":
                metadata = ScanMetadata(collection, 1, 1, Cursor(STAMP, 99), Cursor(STAMP, 99), True, 2)
                return ScanResult(({"id": 99, "sagstrinid": None, "opdateringsdato": STAMP},), metadata)
            return original(collection, **kwargs)

        client.scan = scan
        with patch("collectors.folketinget_sync.load_cases", return_value=self.hydration_result()):
            result = collect(client, mode="historical")
        unresolved = next(item for item in result["diagnostics"] if item["code"] == "unresolved_vote")
        self.assertFalse(unresolved["blocking"])
        self.assertTrue(result["complete"])


if __name__ == "__main__":
    unittest.main()
