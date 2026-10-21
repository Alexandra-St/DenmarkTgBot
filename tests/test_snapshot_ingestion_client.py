from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from io import StringIO
from unittest.mock import patch

from collectors import ingestion_client
from collectors.snapshot_ingestion_client import (
    HISTORICAL_LIVE_ACTIVATION_MODE, MAX_HISTORICAL_COMMIT_ATTEMPTS, SnapshotIngestionError,
    dry_run_prepared_historical_records, submit_prepared_historical_records,
    ingest_snapshot_candidates, main, validate_snapshot_identity,
)
from tests.test_ingestion_client import oda_output

BASE = {"backup_sha256": "a" * 64, "backup_finish_date": "2026-10-17 00:00:00.000", "schema_fingerprint": "c" * 64}
TARGET = {"backup_sha256": "b" * 64, "backup_finish_date": "2026-10-18 00:00:00.000", "schema_fingerprint": "c" * 64}
RUN_ID = "12345678-1234-4234-8234-123456789abc"


def bundle(count: int = 1, complete: bool = True) -> dict[str, object]:
    source = oda_output()["cases"][0]
    cases = [{**source, "external_id": str(1000 + i), "title": f"Forslag {i}"} for i in range(count)]
    return {"source": "folketinget", "complete": complete, "cases": cases}


class Response:
    def __init__(self, value: object) -> None:
        self.raw = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, _size: int = -1) -> bytes:
        return self.raw


class QueueOpener:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=0):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return Response(response)


def start_response(**overrides: object) -> dict[str, object]:
    return {"accepted": True, "run_id": RUN_ID, "status": "OPEN", "state_kind": "snapshot",
            "expected_batches": 1, **overrides}


def commit_response(**overrides: object) -> dict[str, object]:
    return {"run_id": RUN_ID, "status": "COMMITTED", "processed_records": 1, "checkpoints_advanced": False,
            "snapshot_advanced": True, "snapshot_generation": 2, **overrides}


class SnapshotIngestionClientTests(unittest.TestCase):
    def live(self, data=None, opener=None, **kwargs):
        return ingest_snapshot_candidates(
            bundle() if data is None else data, mode="incremental", base_snapshot=BASE,
            target_snapshot=TARGET, endpoint="https://example.test", token="DO_NOT_LOG_TOKEN",
            run_id=RUN_ID, dry_run=False, opener=opener, **kwargs,
        )

    def test_identity_validation_is_strict(self) -> None:
        self.assertEqual(validate_snapshot_identity(BASE).backup_finish_date, BASE["backup_finish_date"])
        invalid = [
            {**BASE, "backup_sha256": "A" * 64},
            {**BASE, "backup_finish_date": "2026-02-30 00:00:00.000"},
            {**BASE, "extra": "field"},
        ]
        for value in invalid:
            with self.assertRaisesRegex(SnapshotIngestionError, "snapshot_identity_invalid"):
                validate_snapshot_identity(value)

    def test_dry_run_is_aggregate_only_and_never_networks(self) -> None:
        result = ingest_snapshot_candidates(bundle(), mode="incremental", base_snapshot=BASE,
                                             target_snapshot=TARGET, dry_run=True,
                                             opener=lambda *_a, **_k: self.fail("network called"))
        self.assertEqual((result["records"], result["batches"]), (1, 1))
        self.assertTrue(result["submission_eligible"])
        rendered = json.dumps(result)
        self.assertNotIn("cases", result)
        self.assertNotIn("Forslag", rendered)
        self.assertNotIn("1000", rendered)

    def test_cli_dry_run_never_networks_even_when_credentials_are_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle_path = Path(directory) / "candidates.json"
            target_path = Path(directory) / "target.json"
            bundle_path.write_text(json.dumps(bundle()), encoding="utf-8")
            target_path.write_text(json.dumps(TARGET), encoding="utf-8")
            output = StringIO()
            with patch.dict(os.environ, {"INGEST_API_URL": "https://example.test", "INGEST_API_TOKEN": "DO_NOT_LOG_TOKEN"}), \
                 redirect_stdout(output):
                code = main([str(bundle_path), "--mode", "historical", "--target-snapshot", str(target_path)],
                            opener=lambda *_a, **_k: self.fail("CLI dry-run called network"))
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result["submission_eligible"])
        self.assertNotIn("DO_NOT_LOG_TOKEN", output.getvalue())

    def test_zero_candidate_dry_run_uses_existing_empty_batch(self) -> None:
        result = ingest_snapshot_candidates({"source": "folketinget", "complete": True, "cases": []},
                                            mode="incremental", base_snapshot=BASE, target_snapshot=TARGET)
        self.assertEqual((result["records"], result["batches"]), (0, 1))

    def test_candidate_validation_errors_use_safe_fixed_categories(self) -> None:
        private_values = (
            "PRIVATE-ID-998",
            "PRIVATE TITLE FROM SOURCE",
            "https://private.example/source/998",
        )
        invalid_cases: list[tuple[dict[str, object], str]] = []

        invalid_identity = bundle()
        invalid_identity["cases"][0].update({
            "external_id": private_values[0],
            "title": private_values[1],
            "source_url": private_values[2],
        })
        invalid_cases.append((invalid_identity, "candidate_identity_or_title_invalid"))

        missing_url = bundle()
        missing_url["cases"][0].update({"title": private_values[1], "source_url": None})
        invalid_cases.append((missing_url, "candidate_source_url_missing"))

        oversized = bundle()
        oversized["cases"][0]["title"] = private_values[1] + ("X" * ingestion_client.MAX_BATCH_BYTES)
        invalid_cases.append((oversized, "candidate_record_too_large"))

        for data, expected_code in invalid_cases:
            with self.subTest(expected_code=expected_code):
                stdout = StringIO()
                stderr = StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SnapshotIngestionError) as caught:
                    ingest_snapshot_candidates(data, mode="incremental", base_snapshot=BASE,
                                               target_snapshot=TARGET, dry_run=True)
                self.assertEqual(caught.exception.code, expected_code)
                self.assertIn(caught.exception.stage, {"prepare", "batch"})
                safe_output = str(caught.exception) + stdout.getvalue() + stderr.getvalue()
                for private_value in private_values:
                    self.assertNotIn(private_value, safe_output)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")

                if expected_code == "candidate_record_too_large":
                    prepared = ingestion_client.prepare_records({"cases": data["cases"]}, "folketinget", "incremental")
                    from collectors.folketinget_relationship_summary import compact_folketinget_record
                    record = compact_folketinget_record(prepared[0])
                    encoded = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                    details = caught.exception.details
                    self.assertEqual(details["oversized_record_count"], 1)
                    self.assertEqual(details["max_record_bytes"], encoded(record))
                    self.assertEqual(details["max_snapshot_bytes"], encoded(record["normalized_snapshot"]))
                    self.assertEqual(details["max_source_metadata_bytes"], encoded(record["source_metadata"]))
                    self.assertEqual(details["max_diagnostics_bytes"], encoded(record["diagnostics"]))
                    summary = record["normalized_snapshot"]["relationship_summary"]
                    self.assertEqual(details["max_document_count"], summary["documents"]["count"])
                    self.assertEqual(details["max_stage_count"], summary["stages"]["count"])
                    self.assertEqual(details["max_vote_count"], summary["votes"]["count"])
                    self.assertEqual(details["worker_snapshot_limit_exceeded_count"], 1)
                    self.assertEqual(details["worker_request_limit_exceeded_count"], 1)

    def test_oversized_cli_diagnostics_are_aggregate_only(self) -> None:
        private_title = "PRIVATE TITLE FROM SOURCE" + ("X" * (300 * 1024))
        data = bundle()
        data["cases"][0]["title"] = private_title
        with tempfile.TemporaryDirectory() as directory:
            bundle_path = Path(directory) / "candidates.json"
            base_path = Path(directory) / "base.json"
            target_path = Path(directory) / "target.json"
            bundle_path.write_text(json.dumps(data), encoding="utf-8")
            base_path.write_text(json.dumps(BASE), encoding="utf-8")
            target_path.write_text(json.dumps(TARGET), encoding="utf-8")
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = main([str(bundle_path), "--mode", "incremental", "--base-snapshot", str(base_path),
                             "--target-snapshot", str(target_path)])
        self.assertEqual(code, 2)
        diagnostic = json.loads(stderr.getvalue())
        self.assertEqual(diagnostic["error"], "candidate_record_too_large")
        self.assertEqual(diagnostic["oversized_record_count"], 1)
        self.assertEqual(diagnostic["worker_snapshot_limit_exceeded_count"], 1)
        self.assertEqual(diagnostic["worker_request_limit_exceeded_count"], 1)
        self.assertNotIn("external_id", diagnostic)
        self.assertNotIn("title", diagnostic)
        self.assertNotIn("url", diagnostic)
        self.assertNotIn("PRIVATE", stderr.getvalue())

    def test_1230_documents_are_classified_full_then_compacted_below_limits(self) -> None:
        data = bundle()
        case = data["cases"][0]
        case["documents"] = [
            {
                "id": index,
                "link_id": 10_000 + index,
                "title": "Bilag om fordrevne fra Ukraine " + ("X" * 300),
                "date_raw": "2026-10-20",
                "released_at_raw": "2026-10-20",
                "document_released_at_raw": "2026-10-20",
                "source_updated_at_raw": "2026-10-21T00:00:00",
                "updated_at_utc": None,
                "link_source_updated_at_raw": "2026-10-21T00:00:00",
                "link_updated_at_utc": None,
            }
            for index in range(1, 1231)
        ]
        full_record = ingestion_client.prepare_records({"cases": [case]}, "folketinget", "incremental")[0]
        full_bytes = len(json.dumps(full_record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self.assertGreater(full_bytes, 585_000)

        result = ingest_snapshot_candidates(data, mode="incremental", base_snapshot=BASE,
                                            target_snapshot=TARGET, dry_run=True)
        self.assertEqual((result["records"], result["batches"]), (1, 1))
        self.assertTrue(result["submission_eligible"])

        from collectors.folketinget_relationship_summary import compact_folketinget_record
        compact = compact_folketinget_record(full_record)
        request_bytes = len(json.dumps({"records": [compact]}, ensure_ascii=False,
                                       separators=(",", ":")).encode("utf-8"))
        snapshot_bytes = len(json.dumps(compact["normalized_snapshot"], ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8"))
        self.assertLess(request_bytes, ingestion_client.MAX_BATCH_BYTES)
        self.assertLess(snapshot_bytes, 128 * 1024)
        self.assertEqual(compact["normalized_snapshot"]["relationship_summary"]["documents"]["count"], 1230)
        self.assertNotIn("documents", compact["normalized_snapshot"])

    def test_unknown_candidate_preparation_error_does_not_leak_details(self) -> None:
        private_error = "PRIVATE-ID-998 PRIVATE TITLE https://private.example/source/998"
        stdout = StringIO()
        stderr = StringIO()
        with patch("collectors.snapshot_ingestion_client.ingestion_client.prepare_records",
                   side_effect=ingestion_client.IngestionError(private_error)), \
             redirect_stdout(stdout), redirect_stderr(stderr), \
             self.assertRaises(SnapshotIngestionError) as caught:
            ingest_snapshot_candidates(bundle(), mode="incremental", base_snapshot=BASE,
                                       target_snapshot=TARGET, dry_run=True)
        self.assertEqual(caught.exception.code, "candidate_preparation_failed")
        self.assertEqual(caught.exception.stage, "prepare")
        safe_output = str(caught.exception) + stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(private_error, safe_output)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_incremental_snapshot_tombstone_is_counted_and_batched_without_classification(self) -> None:
        data = {"source": "folketinget", "complete": True, "cases": [],
                "tombstones": [{"operation": "SOURCE_DELETE", "source": "folketinget", "external_id": "42"}]}
        result = ingest_snapshot_candidates(data, mode="incremental", base_snapshot=BASE,
                                            target_snapshot=TARGET, dry_run=True)
        self.assertEqual((result["records"], result["batches"]), (1, 1))
        self.assertEqual(result["relevance_counts"], {"DIRECT": 0, "POTENTIAL": 0, "IRRELEVANT": 0})
        self.assertTrue(result["submission_eligible"])

        mixed = {**bundle(), "tombstones": [{"operation": "SOURCE_DELETE", "source": "folketinget", "external_id": "42"}]}
        mixed_result = ingest_snapshot_candidates(mixed, mode="incremental", base_snapshot=BASE,
                                                  target_snapshot=TARGET, dry_run=True)
        self.assertEqual((mixed_result["records"], mixed_result["batches"]), (2, 1))
        self.assertEqual(sum(mixed_result["relevance_counts"].values()), 1)

    def test_tombstones_are_strict_and_incremental_folketinget_only(self) -> None:
        invalid = [
            {"operation": "SOURCE_DELETE", "source": "hoeringsportalen", "external_id": "42"},
            {"operation": "SOURCE_DELETE", "source": "folketinget", "external_id": "not-an-id"},
            {"operation": "SOURCE_DELETE", "source": "folketinget", "external_id": "42", "title": "invented"},
        ]
        for tombstone in invalid:
            with self.assertRaisesRegex(SnapshotIngestionError, "tombstone_invalid"):
                ingest_snapshot_candidates({"source": "folketinget", "complete": True, "cases": [],
                                            "tombstones": [tombstone]}, mode="incremental",
                                           base_snapshot=BASE, target_snapshot=TARGET)
        with self.assertRaisesRegex(SnapshotIngestionError, "tombstone_mode_unsupported"):
            ingest_snapshot_candidates({"source": "folketinget", "complete": True, "cases": [],
                                        "tombstones": [{"operation": "SOURCE_DELETE", "source": "folketinget", "external_id": "42"}]},
                                       mode="historical", base_snapshot=None, target_snapshot=TARGET)

    def test_historical_dry_run_allowed_but_ineligible(self) -> None:
        result = ingest_snapshot_candidates(bundle(), mode="historical", base_snapshot=None,
                                            target_snapshot=TARGET, dry_run=True)
        self.assertFalse(result["submission_eligible"])

    def test_prepared_historical_handoff_dry_run_validates_and_never_networks(self) -> None:
        records = ingestion_client.prepare_records(bundle(2), "folketinget", "historical")
        records[0]["relevance"] = "DIRECT"
        records[1]["relevance"] = "POTENTIAL"
        records[1]["historical"] = True
        result = dry_run_prepared_historical_records(
            records, base_snapshot=None, target_snapshot=TARGET
        )
        self.assertEqual((result["records"], result["batches"]), (2, 1))
        self.assertEqual(result["relevance_counts"], {"DIRECT": 1, "POTENTIAL": 1, "IRRELEVANT": 0})
        self.assertTrue(result["submission_eligible"])
        self.assertIsNone(result["base_snapshot"])
        self.assertEqual(result["target_snapshot"], TARGET)
        self.assertNotIn("cases", result)

    def test_prepared_historical_handoff_rejects_bad_contract_without_http(self) -> None:
        record = ingestion_client.prepare_records(bundle(), "folketinget", "historical")[0]
        record["relevance"] = "DIRECT"
        for field, value, category in (
            ("historical", False, "prepared_record_invalid"),
            ("complete", False, "prepared_record_invalid"),
            ("relevance", "IRRELEVANT", "prepared_record_invalid"),
            ("ruleset_version", "unknown", "prepared_record_invalid"),
            ("url", "https://example.invalid/item", "prepared_record_invalid"),
        ):
            invalid = {**record, field: value}
            with self.subTest(field=field), self.assertRaisesRegex(SnapshotIngestionError, category):
                dry_run_prepared_historical_records([invalid], base_snapshot=None, target_snapshot=TARGET)
        with self.assertRaisesRegex(SnapshotIngestionError, "snapshot_base_must_be_null"):
            dry_run_prepared_historical_records([record], base_snapshot=BASE, target_snapshot=TARGET)
        with self.assertRaisesRegex(SnapshotIngestionError, "invalid_mode"):
            dry_run_prepared_historical_records([record], mode="incremental", base_snapshot=None, target_snapshot=TARGET)
        with self.assertRaisesRegex(SnapshotIngestionError, "historical_snapshot_submission_not_enabled"):
            dry_run_prepared_historical_records([record], dry_run=False, base_snapshot=None, target_snapshot=TARGET)
        with patch("collectors.snapshot_ingestion_client._post", side_effect=AssertionError("HTTP forbidden")):
            dry_run_prepared_historical_records([record], base_snapshot=None, target_snapshot=TARGET)

    def test_historical_live_rejected_before_network(self) -> None:
        with self.assertRaisesRegex(SnapshotIngestionError, "historical_snapshot_submission_not_enabled"):
            ingest_snapshot_candidates(bundle(), mode="historical", base_snapshot=None, target_snapshot=TARGET,
                                       dry_run=False, endpoint="https://example.test", token="token",
                                       opener=lambda *_a, **_k: self.fail("network called"))

    def _ready(self, **overrides):
        return {
            "source": "folketinget", "schema_ready": True,
            "snapshot_state_initialized": False, "snapshot_generation": None,
            "active_ingestion_run": False, "worker_supports_snapshot_ingestion": True,
            "historical_snapshot_start_allowed": True, **overrides,
        }

    def _resume_ready(self, *, initialized=False, generation=None, active=True):
        return self._ready(
            snapshot_state_initialized=initialized,
            snapshot_generation=generation,
            active_ingestion_run=active,
            historical_snapshot_start_allowed=False,
        )

    def _prepared_history(self, count=1):
        records = ingestion_client.prepare_records(bundle(count), "folketinget", "historical")
        for record in records:
            record.update({"historical": True, "relevance": "DIRECT"})
        return records

    def _submit_history(self, records, opener, **kwargs):
        return submit_prepared_historical_records(
            records, activation_mode=HISTORICAL_LIVE_ACTIVATION_MODE,
            mode="historical", state_kind="snapshot", base_snapshot=None,
            target_snapshot=TARGET, endpoint="https://example.test",
            token="DO_NOT_LOG_TOKEN", run_id=RUN_ID, opener=opener, **kwargs,
        )

    def test_prepared_historical_live_happy_path_uses_readiness_then_snapshot_protocol(self) -> None:
        responses = [
            self._ready(), start_response(expected_batches=1),
            {"accepted": True, "run_id": RUN_ID, "batch_index": 0, "accepted_records": 1},
            commit_response(snapshot_generation=1, processed_records=1),
        ]
        opener = QueueOpener(responses)
        records = self._prepared_history()
        with patch.object(ingestion_client, "prepare_records", side_effect=AssertionError("must not reprepare")), \
             patch.object(ingestion_client, "upload", side_effect=AssertionError("old uploader forbidden")), \
             patch.object(ingestion_client, "proposed_checkpoints", side_effect=AssertionError("OData state forbidden")):
            result = self._submit_history(records, opener)
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(result["processed_records"], 1)
        self.assertEqual(result["snapshot_generation"], 1)
        self.assertEqual(len(opener.requests), 4)
        readiness, start, batch, commit = opener.requests
        self.assertEqual(readiness.get_method(), "GET")
        self.assertTrue(readiness.full_url.endswith("/internal/snapshot-readiness?source=folketinget"))
        self.assertEqual(start.get_method(), "POST")
        start_payload = json.loads(start.data)
        self.assertEqual(start_payload["mode"], "historical")
        self.assertEqual(start_payload["state_kind"], "snapshot")
        self.assertIsNone(start_payload["base_snapshot"])
        self.assertEqual(start_payload["target_snapshot"], TARGET)
        self.assertEqual(start_payload["run_id"], RUN_ID)
        self.assertEqual(batch.get_method(), "POST")
        self.assertEqual(json.loads(commit.data), {"action": "commit", "run_id": RUN_ID})
        self.assertTrue(all("notification" not in request.full_url for request in opener.requests))

    def test_readiness_blocks_before_any_ingestion_write(self) -> None:
        error = urllib.error.HTTPError("https://example.test", 409, "conflict", {},
                                       io.BytesIO(b'{"error":"snapshot_state_already_initialized"}'))
        opener = QueueOpener([self._resume_ready(initialized=True, generation=1, active=False), error])
        with self.assertRaisesRegex(SnapshotIngestionError, "snapshot_state_already_initialized") as raised:
            self._submit_history(self._prepared_history(), opener)
        self.assertEqual(raised.exception.stage, "start")
        self.assertEqual([request.get_method() for request in opener.requests], ["GET", "POST"])

    def test_ambiguous_start_retry_resumes_same_open_run_idempotently(self) -> None:
        records = self._prepared_history()
        # Model a timeout after the Worker accepted start but before its response
        # reached the client. The retry must prove the same stored run by hash.
        first = QueueOpener([self._ready(), TimeoutError("ambiguous")])
        with self.assertRaisesRegex(SnapshotIngestionError, "ingestion_transport_failed"):
            self._submit_history(records, first)
        second = QueueOpener([
            self._resume_ready(), start_response(idempotent=True),
            {"accepted": True, "run_id": RUN_ID, "batch_index": 0, "accepted_records": 1},
            commit_response(snapshot_generation=1, processed_records=1),
        ])
        result = self._submit_history(records, second)
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(result["accepted_records"], 1)
        self.assertTrue(result["start_idempotent"])
        self.assertEqual([json.loads(request.data).get("run_id") for request in first.requests[1:]], [RUN_ID])
        self.assertEqual([json.loads(request.data).get("run_id") for request in second.requests[1:]], [RUN_ID] * 3)

    def test_committing_resume_does_not_resend_batches_and_counts_prior_acceptance(self) -> None:
        records = self._prepared_history(21)
        batches = ingestion_client.chunks(records)
        opener = QueueOpener([
            self._resume_ready(), start_response(status="COMMITTING", expected_batches=len(batches), idempotent=True),
            {"run_id": RUN_ID, "status": "COMMITTING", "processed_records": 20, "remaining_records": 1,
             "checkpoints_advanced": False, "snapshot_advanced": False},
            commit_response(snapshot_generation=1, processed_records=1),
        ])
        result = self._submit_history(records, opener)
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(result["accepted_records"], 21)
        self.assertEqual(result["processed_records"], 21)
        self.assertTrue(result["start_idempotent"])
        self.assertEqual([json.loads(request.data).get("action") for request in opener.requests[1:]],
                         ["start", "commit", "commit"])

    def test_committed_resume_reports_logical_accepted_count(self) -> None:
        opener = QueueOpener([
            self._resume_ready(initialized=True, generation=1, active=False),
            start_response(status="COMMITTED", idempotent=True),
            commit_response(snapshot_generation=1, processed_records=0),
        ])
        result = self._submit_history(self._prepared_history(), opener)
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(result["accepted_records"], 1)
        self.assertEqual(result["processed_records"], 0)
        self.assertTrue(result["start_idempotent"])
        self.assertEqual([request.get_method() for request in opener.requests], ["GET", "POST", "POST"])

    def test_historical_commit_loop_exceeds_old_ten_request_ceiling(self) -> None:
        records = self._prepared_history(221)
        batches = ingestion_client.chunks(records)
        responses = [self._ready(), start_response(expected_batches=len(batches), idempotent=False)]
        responses.extend({"accepted": True, "run_id": RUN_ID, "batch_index": index,
                          "accepted_records": len(batch)} for index, batch in enumerate(batches))
        responses.extend(
            {"run_id": RUN_ID, "status": "COMMITTING", "processed_records": 20,
             "remaining_records": 221 - 20 * (index + 1),
             "checkpoints_advanced": False, "snapshot_advanced": False}
            for index in range(11)
        )
        responses.append(commit_response(snapshot_generation=1, processed_records=1))
        opener = QueueOpener(responses)
        result = self._submit_history(records, opener)
        commit_requests = [request for request in opener.requests
                           if request.get_method() == "POST" and json.loads(request.data)["action"] == "commit"]
        self.assertEqual(len(commit_requests), 12)
        self.assertEqual(result["accepted_records"], 221)
        self.assertEqual(result["processed_records"], 221)
        self.assertFalse(result["commit_resumed"])

    def test_1265_record_historical_commit_progression_fits_derived_bound(self) -> None:
        records = self._prepared_history(1265)
        batches = ingestion_client.chunks(records)
        expected_attempts = (len(records) + 20 - 1) // 20
        self.assertEqual(expected_attempts, 64)
        self.assertEqual(expected_attempts, (1265 + 20 - 1) // 20)
        responses = [self._ready(), start_response(expected_batches=len(batches), idempotent=False)]
        responses.extend({"accepted": True, "run_id": RUN_ID, "batch_index": index,
                          "accepted_records": len(batch)} for index, batch in enumerate(batches))
        responses.extend(
            {"run_id": RUN_ID, "status": "COMMITTING", "processed_records": 20,
             "remaining_records": 1265 - 20 * (index + 1),
             "checkpoints_advanced": False, "snapshot_advanced": False}
            for index in range(63)
        )
        responses.append(commit_response(snapshot_generation=1, processed_records=5))
        opener = QueueOpener(responses)
        result = self._submit_history(records, opener)
        commit_requests = [request for request in opener.requests
                           if request.get_method() == "POST" and json.loads(request.data)["action"] == "commit"]
        self.assertEqual(len(commit_requests), 64)
        self.assertEqual(result["processed_records"], 1265)
        self.assertEqual(result["accepted_records"], 1265)
        self.assertEqual(result["status"], "COMMITTED")

    def test_resumed_committing_historical_run_accepts_partial_current_attempt_count(self) -> None:
        records = self._prepared_history(1265)
        batches = ingestion_client.chunks(records)
        opener = QueueOpener([
            self._resume_ready(), start_response(status="COMMITTING", expected_batches=len(batches), idempotent=True),
            commit_response(snapshot_generation=1, processed_records=5),
        ])
        result = self._submit_history(records, opener)
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(result["records"], 1265)
        self.assertEqual(result["accepted_records"], 1265)
        self.assertEqual(result["processed_records"], 5)
        self.assertTrue(result["commit_resumed"])
        self.assertEqual([json.loads(request.data).get("action") for request in opener.requests[1:]],
                         ["start", "commit"])

    def test_historical_commit_no_progress_fails_closed(self) -> None:
        opener = QueueOpener([
            self._ready(), start_response(idempotent=False),
            {"accepted": True, "run_id": RUN_ID, "batch_index": 0, "accepted_records": 1},
            {"run_id": RUN_ID, "status": "COMMITTING", "processed_records": 0, "remaining_records": 1,
             "checkpoints_advanced": False, "snapshot_advanced": False},
        ])
        with self.assertRaisesRegex(SnapshotIngestionError, "commit_no_progress"):
            self._submit_history(self._prepared_history(), opener)

    def test_repeated_historical_committing_remaining_count_must_decrease(self) -> None:
        records = self._prepared_history(21)
        batches = ingestion_client.chunks(records)
        responses = [self._ready(), start_response(expected_batches=len(batches), idempotent=False)]
        responses.extend({"accepted": True, "run_id": RUN_ID, "batch_index": index,
                          "accepted_records": len(batch)} for index, batch in enumerate(batches))
        responses.extend([
            {"run_id": RUN_ID, "status": "COMMITTING", "processed_records": 20, "remaining_records": 1,
             "checkpoints_advanced": False, "snapshot_advanced": False},
            {"run_id": RUN_ID, "status": "COMMITTING", "processed_records": 20, "remaining_records": 1,
             "checkpoints_advanced": False, "snapshot_advanced": False},
        ])
        with self.assertRaisesRegex(SnapshotIngestionError, "commit_no_progress"):
            self._submit_history(records, QueueOpener(responses))

    def test_historical_commit_override_cannot_exceed_derived_finite_bound(self) -> None:
        self.assertEqual(MAX_HISTORICAL_COMMIT_ATTEMPTS, 500)
        records = self._prepared_history(221)
        batches = ingestion_client.chunks(records)
        responses = [self._ready(), start_response(expected_batches=len(batches), idempotent=False)]
        responses.extend({"accepted": True, "run_id": RUN_ID, "batch_index": index,
                          "accepted_records": len(batch)} for index, batch in enumerate(batches))
        responses.extend(
            {"run_id": RUN_ID, "status": "COMMITTING", "processed_records": 20,
             "remaining_records": 221 - 20 * (index + 1),
             "checkpoints_advanced": False, "snapshot_advanced": False}
            for index in range(2)
        )
        opener = QueueOpener(responses)
        with self.assertRaisesRegex(SnapshotIngestionError, "commit_retry_limit"):
            self._submit_history(records, opener, max_commit_attempts=2)
        self.assertEqual(sum(1 for request in opener.requests if request.get_method() == "POST"
                             and json.loads(request.data)["action"] == "commit"), 2)

    def test_resume_candidate_requires_worker_idempotency_and_never_changes_run_id(self) -> None:
        error = urllib.error.HTTPError("https://example.test", 409, "conflict", {},
                                       io.BytesIO(b'{"error":"source_run_active_or_run_conflict"}'))
        opener = QueueOpener([self._resume_ready(), error])
        with self.assertRaisesRegex(SnapshotIngestionError, "source_run_active_or_run_conflict"):
            self._submit_history(self._prepared_history(), opener)
        self.assertEqual([request.get_method() for request in opener.requests], ["GET", "POST"])
        self.assertEqual(json.loads(opener.requests[-1].data)["run_id"], RUN_ID)

    def test_non_idempotent_new_start_is_rejected_after_nonfresh_readiness(self) -> None:
        opener = QueueOpener([self._resume_ready(), start_response(idempotent=False)])
        with self.assertRaisesRegex(SnapshotIngestionError, "historical_resume_not_confirmed"):
            self._submit_history(self._prepared_history(), opener)
        self.assertEqual([request.get_method() for request in opener.requests], ["GET", "POST"])

    def test_unknown_readiness_state_cannot_be_used_as_resume_candidate(self) -> None:
        opener = QueueOpener([self._ready(snapshot_state_initialized=None, active_ingestion_run=True,
                                          historical_snapshot_start_allowed=False)])
        with self.assertRaisesRegex(SnapshotIngestionError, "snapshot_readiness_blocked"):
            self._submit_history(self._prepared_history(), opener)
        self.assertEqual([request.get_method() for request in opener.requests], ["GET"])

    def test_invalid_activation_and_base_snapshot_block_before_network(self) -> None:
        records = self._prepared_history()
        opener = QueueOpener([])
        with self.assertRaisesRegex(SnapshotIngestionError, "historical_snapshot_submission_not_enabled"):
            submit_prepared_historical_records(
                records, activation_mode="", mode="historical", state_kind="snapshot",
                base_snapshot=None, target_snapshot=TARGET, endpoint="https://example.test",
                token="token", run_id=RUN_ID, opener=opener,
            )
        with self.assertRaisesRegex(SnapshotIngestionError, "snapshot_base_must_be_null"):
            submit_prepared_historical_records(
                records, activation_mode=HISTORICAL_LIVE_ACTIVATION_MODE, mode="historical",
                state_kind="snapshot", base_snapshot=BASE, target_snapshot=TARGET,
                endpoint="https://example.test", token="token", run_id=RUN_ID, opener=opener,
            )
        self.assertEqual(opener.requests, [])

    def test_ambiguous_live_transport_keeps_same_run_id_and_does_not_retry_write(self) -> None:
        opener = QueueOpener([self._ready(), TimeoutError("secret or request details")])
        with self.assertRaisesRegex(SnapshotIngestionError, "ingestion_transport_failed") as raised:
            self._submit_history(self._prepared_history(), opener)
        self.assertEqual(raised.exception.run_id, RUN_ID)
        self.assertEqual(raised.exception.stage, "start")
        self.assertEqual([request.get_method() for request in opener.requests], ["GET", "POST"])
        self.assertEqual(len(opener.requests), 2)

    def test_readiness_response_shape_and_disabled_snapshot_state_fail_closed(self) -> None:
        for payload in (
            self._ready(active_ingestion_run=True),
            self._ready(worker_supports_snapshot_ingestion=False),
            {**self._ready(), "unexpected": True},
        ):
            with self.subTest(payload=payload), self.assertRaises(SnapshotIngestionError):
                self._submit_history(self._prepared_history(), QueueOpener([payload]))

    def test_prepared_historical_live_enforces_record_batch_and_byte_limits_before_readiness(self) -> None:
        records = self._prepared_history()
        for patch_target, patch_value, category in (
            ("MAX_HISTORICAL_SELECTED_RECORDS", 0, "prepared_record_limit_exceeded"),
            ("MAX_HISTORICAL_BATCHES", 0, "prepared_batch_limit_exceeded"),
            ("MAX_HISTORICAL_SELECTED_BYTES", 1, "prepared_record_limit_exceeded"),
        ):
            opener = QueueOpener([])
            with self.subTest(category=category), patch(
                f"collectors.snapshot_ingestion_client.{patch_target}", patch_value
            ), self.assertRaisesRegex(SnapshotIngestionError, category):
                self._submit_history(records, opener)
            self.assertEqual(opener.requests, [])

    def test_incomplete_incremental_live_rejected_before_network(self) -> None:
        with self.assertRaisesRegex(SnapshotIngestionError, "incomplete_candidates"):
            self.live(bundle(complete=False), opener=lambda *_a, **_k: self.fail("network called"))

    def test_prepare_and_chunks_reused_old_checkpoint_helpers_never_called(self) -> None:
        with patch.object(ingestion_client, "prepare_records", wraps=ingestion_client.prepare_records) as prepare, \
             patch.object(ingestion_client, "chunks", wraps=ingestion_client.chunks) as make_chunks, \
             patch.object(ingestion_client, "upload", side_effect=AssertionError("old uploader called")), \
             patch.object(ingestion_client, "collection_complete", side_effect=AssertionError("old helper called")), \
             patch.object(ingestion_client, "proposed_checkpoints", side_effect=AssertionError("old helper called")):
            ingest_snapshot_candidates(bundle(), mode="incremental", base_snapshot=BASE,
                                        target_snapshot=TARGET, dry_run=True)
        prepare.assert_called_once()
        make_chunks.assert_called_once()

    def test_empty_live_batch_is_submitted_then_snapshot_committed(self) -> None:
        opener = QueueOpener([
            start_response(),
            {"accepted": True, "run_id": RUN_ID, "batch_index": 0, "accepted_records": 0},
            commit_response(),
        ])
        result = self.live({"source": "folketinget", "complete": True, "cases": []}, opener)
        payloads = [json.loads(r.data) for r in opener.requests]
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(payloads[0]["state_kind"], "snapshot")
        self.assertEqual(payloads[0]["expected_batches"], 1)
        self.assertNotIn("proposed_checkpoints", payloads[0])
        self.assertEqual(payloads[1]["records"], [])
        self.assertEqual(payloads[2], {"action": "commit", "run_id": RUN_ID})

    def test_multi_batch_live_submission(self) -> None:
        data = bundle(51)
        records = ingestion_client.prepare_records({"cases": data["cases"]}, "folketinget", "incremental")
        expected_batches = ingestion_client.chunks(records)
        responses = [start_response(expected_batches=len(expected_batches))]
        responses += [{"accepted": True, "run_id": RUN_ID, "batch_index": i, "accepted_records": len(batch)}
                      for i, batch in enumerate(expected_batches)]
        responses.append(commit_response())
        opener = QueueOpener(responses)
        result = self.live(data, opener)
        self.assertEqual(result["batches"], len(expected_batches))
        self.assertEqual(result["accepted_records"], 51)
        self.assertEqual(len(opener.requests), len(expected_batches) + 2)
        self.assertTrue(all("source-states" not in r.full_url for r in opener.requests))

    def test_incremental_commit_limit_is_derived_from_record_count(self) -> None:
        data = bundle(221)
        records = ingestion_client.prepare_records({"cases": data["cases"]}, "folketinget", "incremental")
        batches = ingestion_client.chunks(records)
        responses = [start_response(expected_batches=len(batches))]
        responses.extend({"accepted": True, "run_id": RUN_ID, "batch_index": index,
                          "accepted_records": len(batch)} for index, batch in enumerate(batches))
        responses.extend({"run_id": RUN_ID, "status": "COMMITTING", "processed_records": 20,
                          "remaining_records": 221 - 20 * (index + 1),
                          "checkpoints_advanced": False, "snapshot_advanced": False}
                         for index in range(11))
        responses.append(commit_response(processed_records=1))
        opener = QueueOpener(responses)
        result = self.live(data, opener)
        commit_requests = [request for request in opener.requests
                           if json.loads(request.data).get("action") == "commit"]
        self.assertEqual(len(commit_requests), 12)
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(result["records"], 221)

    def test_incremental_commit_no_progress_fails_closed(self) -> None:
        opener = QueueOpener([
            start_response(),
            {"accepted": True, "run_id": RUN_ID, "batch_index": 0, "accepted_records": 2},
            {"run_id": RUN_ID, "status": "COMMITTING", "processed_records": 0, "remaining_records": 2,
             "checkpoints_advanced": False, "snapshot_advanced": False},
        ])
        with self.assertRaisesRegex(SnapshotIngestionError, "commit_no_progress"):
            self.live(bundle(2), opener)

    def test_incremental_commit_bound_is_finite_and_checked_before_network(self) -> None:
        opener = QueueOpener([])
        with patch("collectors.snapshot_ingestion_client.MAX_INCREMENTAL_COMMIT_ATTEMPTS", 1):
            with self.assertRaisesRegex(SnapshotIngestionError, "prepared_batch_limit_exceeded"):
                self.live(bundle(21), opener)
        self.assertEqual(opener.requests, [])

    def test_incremental_retry_resumes_same_run_without_resending_batches(self) -> None:
        first = QueueOpener([
            start_response(),
            {"accepted": True, "run_id": RUN_ID, "batch_index": 0, "accepted_records": 21},
            TimeoutError("ambiguous commit response"),
        ])
        with self.assertRaisesRegex(SnapshotIngestionError, "ingestion_transport_failed"):
            self.live(bundle(21), first)
        resumed = QueueOpener([
            start_response(status="COMMITTING", expected_batches=1, idempotent=True),
            commit_response(processed_records=1),
        ])
        result = self.live(bundle(21), resumed)
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual([json.loads(request.data)["action"] for request in resumed.requests], ["start", "commit"])
        self.assertEqual([json.loads(request.data)["run_id"] for request in first.requests + resumed.requests],
                         [RUN_ID] * 5)

    def test_committing_retries_same_run_id(self) -> None:
        opener = QueueOpener([
            start_response(),
            {"accepted": True, "run_id": RUN_ID, "batch_index": 0, "accepted_records": 21},
            {"run_id": RUN_ID, "status": "COMMITTING", "processed_records": 20, "remaining_records": 1,
             "checkpoints_advanced": False, "snapshot_advanced": False},
            commit_response(processed_records=1),
        ])
        self.assertEqual(self.live(bundle(21), opener)["status"], "COMMITTED")
        self.assertEqual([json.loads(r.data)["run_id"] for r in opener.requests], [RUN_ID] * 4)

    def test_blocked_response_does_not_claim_advance(self) -> None:
        opener = QueueOpener([
            start_response(),
            {"accepted": True, "run_id": RUN_ID, "batch_index": 0, "accepted_records": 1},
            {"run_id": RUN_ID, "status": "BLOCKED", "checkpoints_advanced": False, "snapshot_advanced": False},
        ])
        result = self.live(opener=opener)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["snapshot_advanced"])

    def test_commit_requires_snapshot_only_advancement(self) -> None:
        opener = QueueOpener([
            start_response(),
            {"accepted": True, "run_id": RUN_ID, "batch_index": 0, "accepted_records": 1},
            commit_response(checkpoints_advanced=True),
        ])
        with self.assertRaisesRegex(SnapshotIngestionError, "worker_response_invalid"):
            self.live(opener=opener)

    def test_partial_failure_keeps_run_id_and_safe_stage(self) -> None:
        opener = QueueOpener([start_response(), TimeoutError()])
        with self.assertRaises(SnapshotIngestionError) as caught:
            self.live(opener=opener)
        self.assertEqual((caught.exception.code, caught.exception.run_id, caught.exception.stage),
                         ("ingestion_transport_failed", RUN_ID, "batch"))
        self.assertNotIn("DO_NOT_LOG_TOKEN", str(caught.exception))

    def test_worker_errors_are_allowlisted_and_do_not_leak_body(self) -> None:
        error = urllib.error.HTTPError("https://example.test", 409, "oops", {},
                                       io.BytesIO(b'{"error":"snapshot_base_stale","title":"PRIVATE TITLE"}'))
        with self.assertRaises(SnapshotIngestionError) as caught:
            self.live(opener=QueueOpener([error]))
        self.assertEqual(caught.exception.code, "snapshot_base_stale")
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertNotIn("DO_NOT_LOG_TOKEN", str(caught.exception))

    def test_idempotent_committed_run_resumes_with_same_id(self) -> None:
        opener = QueueOpener([
            start_response(status="COMMITTED", idempotent=True),
            commit_response(processed_records=0),
        ])
        result = self.live(opener=opener)
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual([json.loads(r.data)["run_id"] for r in opener.requests], [RUN_ID, RUN_ID])

    def test_malformed_worker_shapes_fail_closed(self) -> None:
        opener = QueueOpener([start_response(expected_batches=99)])
        with self.assertRaisesRegex(SnapshotIngestionError, "worker_response_invalid"):
            self.live(opener=opener)

    def test_pair_validation_rejects_regression_equal_conflict_and_schema_change(self) -> None:
        cases = [
            (BASE, {**BASE, "backup_finish_date": "2026-10-16 00:00:00.000"}, "snapshot_regression"),
            (BASE, {**BASE, "backup_sha256": "d" * 64}, "snapshot_identity_conflict"),
            (BASE, {**TARGET, "schema_fingerprint": "d" * 64}, "snapshot_schema_changed"),
        ]
        for base, target, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(SnapshotIngestionError, code):
                ingest_snapshot_candidates(bundle(), mode="incremental", base_snapshot=base,
                                           target_snapshot=target, dry_run=True)


if __name__ == "__main__":
    unittest.main()
