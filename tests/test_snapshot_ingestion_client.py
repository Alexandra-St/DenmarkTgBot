from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from io import StringIO
from unittest.mock import patch

from collectors import ingestion_client
from collectors.snapshot_ingestion_client import (
    SnapshotIngestionError, dry_run_prepared_historical_records,
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

    def read(self) -> bytes:
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

    def test_committing_retries_same_run_id(self) -> None:
        opener = QueueOpener([
            start_response(),
            {"accepted": True, "run_id": RUN_ID, "batch_index": 0, "accepted_records": 1},
            {"run_id": RUN_ID, "status": "COMMITTING", "processed_records": 1, "remaining_records": 0,
             "checkpoints_advanced": False, "snapshot_advanced": False},
            commit_response(),
        ])
        self.assertEqual(self.live(opener=opener)["status"], "COMMITTED")
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
