from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock
import urllib.error
from types import SimpleNamespace


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "folketinget_backup_poc", ROOT / "tools" / "folketinget_backup_poc.py"
)
POC = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(POC)


def create_candidate_manifest(
    path: pathlib.Path, *, backup: str, finish: str, include_sag: bool,
    extra_records: dict[str, list[tuple[str, bytes, bytes]]] | None = None,
    schema_fingerprint: str = "a" * 64,
) -> None:
    path.unlink(missing_ok=True)
    entities = POC.REQUIRED_ENTITIES
    records = {entity: [] for entity in entities}
    if include_sag:
        key = json.dumps(
            {"entity": "Sag", "key": [["id", "int", 42]]},
            sort_keys=True,
            separators=(",", ":"),
        )
        records["Sag"].append((key, b"f" * 32, b"s" * 32))
    for entity, items in (extra_records or {}).items():
        records[entity].extend(items)
    total = sum(map(len, records.values()))
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE snapshot_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), backup_sha256 TEXT NOT NULL, "
            "backup_finish_date TEXT NOT NULL, source_sql_major INTEGER NOT NULL, schema_fingerprint TEXT NOT NULL, "
            "generated_at TEXT NOT NULL, total_rows INTEGER NOT NULL);"
            "CREATE TABLE entity_counts (entity TEXT PRIMARY KEY, row_count INTEGER NOT NULL) WITHOUT ROWID;"
            'CREATE TABLE "rows" (entity TEXT NOT NULL, canonical_key TEXT NOT NULL, full_hash BLOB NOT NULL CHECK(length(full_hash)=32), '
            "semantic_hash BLOB NOT NULL CHECK(length(semantic_hash)=32), PRIMARY KEY(entity, canonical_key)) WITHOUT ROWID;"
        )
        db.execute(
            "INSERT INTO snapshot_metadata VALUES (1, ?, ?, 17, ?, ?, ?)",
            (backup * 64, finish, schema_fingerprint, "2026-10-19T00:00:00Z", total),
        )
        for entity in entities:
            db.execute("INSERT INTO entity_counts VALUES (?, ?)", (entity, len(records[entity])))
            db.executemany('INSERT INTO "rows" VALUES (?, ?, ?, ?)',
                           ((entity, key, full, semantic) for key, full, semantic in records[entity]))


def create_v2_candidate_manifest(
    path: pathlib.Path, *, backup: str, finish: str,
    rows: dict[str, list[tuple[int, bytes, bytes, dict[str, int | None]]]],
    schema_fingerprint: str = "a" * 64,
) -> None:
    """Create a structurally valid synthetic v2 manifest with routing metadata."""
    path.unlink(missing_ok=True)
    total = sum(len(rows.get(entity, ())) for entity in POC.REQUIRED_ENTITIES)
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE snapshot_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), backup_sha256 TEXT NOT NULL, "
            "backup_finish_date TEXT NOT NULL, source_sql_major INTEGER NOT NULL, schema_fingerprint TEXT NOT NULL, "
            "generated_at TEXT NOT NULL, total_rows INTEGER NOT NULL, manifest_format_version INTEGER NOT NULL, "
            "routing_row_count INTEGER NOT NULL);"
            "CREATE TABLE entity_counts (entity TEXT PRIMARY KEY, row_count INTEGER NOT NULL) WITHOUT ROWID;"
            'CREATE TABLE "rows" (entity TEXT NOT NULL, canonical_key TEXT NOT NULL, full_hash BLOB NOT NULL CHECK(length(full_hash)=32), '
            "semantic_hash BLOB NOT NULL CHECK(length(semantic_hash)=32), PRIMARY KEY(entity, canonical_key)) WITHOUT ROWID;"
            "CREATE TABLE routing (entity TEXT NOT NULL, row_id INTEGER NOT NULL, sagid INTEGER, dokumentid INTEGER, "
            "sagstrinid INTEGER, statusid INTEGER, PRIMARY KEY(entity,row_id)) WITHOUT ROWID;"
            "CREATE INDEX routing_sagid_idx ON routing(sagid) WHERE sagid IS NOT NULL;"
            "CREATE INDEX routing_dokumentid_idx ON routing(dokumentid) WHERE entity='SagDokument' AND dokumentid IS NOT NULL;"
            "CREATE INDEX routing_sagstrinid_idx ON routing(sagstrinid) WHERE entity='Afstemning' AND sagstrinid IS NOT NULL;"
            "CREATE INDEX routing_statusid_idx ON routing(statusid) WHERE entity='Sag' AND statusid IS NOT NULL;"
        )
        db.execute(
            "INSERT INTO snapshot_metadata VALUES (1, ?, ?, 17, ?, ?, ?, 2, ?)",
            (backup * 64, finish, schema_fingerprint, "2026-10-19T00:00:00Z", total, total),
        )
        for entity in POC.REQUIRED_ENTITIES:
            items = rows.get(entity, [])
            db.execute("INSERT INTO entity_counts VALUES (?, ?)", (entity, len(items)))
            for row_id, full_hash, semantic_hash, routing in items:
                key = json.dumps({"entity": entity, "key": [["id", "int", row_id]]}, sort_keys=True, separators=(",", ":"))
                db.execute('INSERT INTO "rows" VALUES (?, ?, ?, ?)', (entity, key, full_hash, semantic_hash))
                db.execute(
                    "INSERT INTO routing VALUES (?, ?, ?, ?, ?, ?)",
                    (entity, row_id, routing.get("sagid"), routing.get("dokumentid"),
                     routing.get("sagstrinid"), routing.get("statusid")),
                )


class BackupPocSafetyTests(unittest.TestCase):
    @staticmethod
    def _candidate_bundle(cases, *, complete=True, diagnostics=None, candidate_count=None):
        return {
            "format_version": 1,
            "source": "folketinget",
            "candidate_case_count": len(cases) if candidate_count is None else candidate_count,
            "cases": cases,
            "complete": complete,
            "diagnostics": diagnostics or [],
            "diff": {
                "entities": {entity: dict.fromkeys(POC.snapshot_diff.CATEGORIES, 0)
                             for entity in POC.REQUIRED_ENTITIES},
                "totals": dict.fromkeys(POC.snapshot_diff.CATEGORIES, 0),
            },
            "_summary": {"affected_case_count": len(cases), "hydrated_case_count": len(cases)},
        }

    def _run_classification(self, root, bundle):
        stamp = "2026-10-19T02:00:00Z"
        old, new = root / "old.sqlite", root / "new.sqlite"
        create_candidate_manifest(old, backup="1", finish="2026-10-18T02:00:00Z", include_sag=False)
        create_candidate_manifest(new, backup="2", finish=stamp, include_sag=False)
        with mock.patch("folketinget_candidate_extraction.extract_candidates", return_value=bundle):
            return POC.run_candidate_extraction(
                old, new, SimpleNamespace(), root, "2026-10-18T02:00:00Z", stamp,
                classify_candidates=True,
            )

    @staticmethod
    def _ingestion_case(external_id, title, *, complete=True):
        return {
            "source": "folketinget",
            "external_id": str(external_id),
            "number": f"L {external_id}",
            "title": title,
            "type_id": 1,
            "category_id": 1,
            "status": {"id": 1, "name": "Fremsat"},
            "source_updated_at_raw": None,
            "updated_at_utc": None,
            "source_url": f"https://oda.ft.dk/api/Sag({external_id})",
            "legal_url": None,
            "stages": [],
            "documents": [],
            "votes": [],
            "complete": complete,
            "historical": False,
            "notify_eligible": None,
        }

    def _run_ingestion_preparation(self, root, cases, *, complete=True, diagnostics=None):
        stamp = "2026-10-19T02:00:00Z"
        old, new = root / "old.sqlite", root / "new.sqlite"
        create_candidate_manifest(old, backup="1", finish="2026-10-18T02:00:00Z", include_sag=False)
        create_candidate_manifest(new, backup="2", finish=stamp, include_sag=False)
        bundle = self._candidate_bundle(cases, complete=complete, diagnostics=diagnostics)
        with mock.patch("folketinget_candidate_extraction.extract_candidates", return_value=bundle):
            result = POC.run_candidate_extraction(
                old, new, SimpleNamespace(), root, "2026-10-18T02:00:00Z", stamp,
                classify_candidates=True, prepare_ingestion=True,
            )
        return result

    def _run_snapshot_client_dry_run(self, root, cases, *, complete=True, diagnostics=None):
        stamp = "2026-10-19 02:00:00.000"
        old, new = root / "old.sqlite", root / "new.sqlite"
        create_candidate_manifest(old, backup="1", finish="2026-10-18 02:00:00.000", include_sag=False)
        create_candidate_manifest(new, backup="2", finish=stamp, include_sag=False)
        context = POC.validate_candidate_manifest_inputs(old, new, "100", "101", require_newer_snapshot=True)
        candidate_bundle = self._candidate_bundle(cases, complete=complete, diagnostics=diagnostics)
        with mock.patch("folketinget_candidate_extraction.extract_candidates", return_value=candidate_bundle):
            return POC.run_candidate_extraction(
                old, new, SimpleNamespace(), root, context["old_finish"], context["new_finish"],
                snapshot_ingestion_dry_run=True,
                base_snapshot=context["base_snapshot"], target_snapshot=context["target_snapshot"],
            )

    def test_snapshot_classification_handles_zero_direct_potential_and_irrelevant(self) -> None:
        from collectors import relevance

        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            zero = self._run_classification(root, self._candidate_bundle([]))
            self.assertEqual(zero["classification"], {
                "candidate_case_count": 0,
                "classified_case_count": 0,
                "relevance_counts": {"DIRECT": 0, "POTENTIAL": 0, "IRRELEVANT": 0},
                "notification_blocked_count": 0,
                "classification_diagnostics_count": 0,
                "ruleset_version": relevance.RULESET_VERSION,
                "extraction_complete": True,
                "complete": True,
                "extraction_diagnostics": [],
            })
            cases = [
                {"source": "folketinget", "historical": False, "notify_eligible": None,
                 "title": "Fordrevne fra Ukraine"},
                {"source": "folketinget", "historical": False, "notify_eligible": None,
                 "title": "Ændring af udlændingeloven om opholdstilladelse"},
                {"source": "folketinget", "historical": False, "notify_eligible": None,
                 "title": "PRIVATE IRRELEVANT TITLE"},
            ]
            result = self._run_classification(root, self._candidate_bundle(cases))
            classification = result["classification"]
            self.assertEqual(classification["relevance_counts"], {"DIRECT": 1, "POTENTIAL": 1, "IRRELEVANT": 1})
            self.assertEqual(classification["classified_case_count"], 3)
            self.assertEqual(classification["ruleset_version"], "1.1")
            output = StringIO()
            with redirect_stdout(output):
                POC.write_candidate_classification_summary(result)
            rendered = output.getvalue()
            for private in ("PRIVATE IRRELEVANT TITLE", "external_id", "Fordrevne fra Ukraine", "documents"):
                self.assertNotIn(private, rendered)
            self.assertIn("Snapshot relevance dry-run: PASS", rendered)

    def test_snapshot_classification_aggregates_blocked_and_diagnostics_and_preserves_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            cases = [
                {"source": "folketinget", "historical": False, "notify_eligible": None,
                 "title": "PRIVATE TITLE", "complete": False},
            ]
            extraction_diagnostics = [{"code": "source_lookup_failed", "count": 1}]
            result = self._run_classification(
                root, self._candidate_bundle(cases, complete=False, diagnostics=extraction_diagnostics)
            )
            classification = result["classification"]
            self.assertEqual(classification["notification_blocked_count"], 1)
            self.assertEqual(classification["classification_diagnostics_count"], 1)
            self.assertFalse(classification["complete"])
            self.assertFalse(classification["extraction_complete"])
            self.assertEqual(classification["extraction_diagnostics"], extraction_diagnostics)
            output = StringIO()
            with redirect_stdout(output):
                POC.write_candidate_classification_summary(result)
            self.assertIn("Snapshot relevance dry-run: INCOMPLETE", output.getvalue())
            self.assertIn("source_lookup_failed: 1", output.getvalue())
            self.assertNotIn("PRIVATE TITLE", output.getvalue())

    def test_snapshot_classification_count_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = self._candidate_bundle([{"title": "PRIVATE"}], candidate_count=2)
            with self.assertRaises(POC.PocError) as raised:
                self._run_classification(pathlib.Path(temp), bundle)
            self.assertEqual(raised.exception.category, "candidate_classification_count_mismatch")

    def test_snapshot_classification_candidate_file_is_private_and_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            bundle = self._candidate_bundle([])
            observed_modes = []
            original = POC._write_private_candidate_bundle

            def inspect_private_file(payload, directory):
                path = original(payload, directory)
                observed_modes.append(path.stat().st_mode & 0o777)
                return path

            with mock.patch.object(POC, "_write_private_candidate_bundle", side_effect=inspect_private_file):
                self._run_classification(root, bundle)
            self.assertEqual(observed_modes, [0o600])
            self.assertEqual(list(root.glob("denmarkbot-candidate-bundle-*.json")), [])

    def test_snapshot_ingestion_preparation_zero_candidates_reports_actual_empty_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            result = self._run_ingestion_preparation(root, [])
            ingestion = result["ingestion"]
            self.assertTrue(ingestion["complete"])
            self.assertEqual(ingestion["prepared_record_count"], 0)
            self.assertEqual(ingestion["batch_count"], 1)
            self.assertEqual(ingestion["largest_batch_record_count"], 0)
            self.assertEqual(ingestion["largest_batch_encoded_bytes"], len(b'{"records":[]}'))
            self.assertEqual(ingestion["empty_batch_representation"], "one empty batch")
            self.assertFalse(ingestion["submission_eligible"])
            output = StringIO()
            with redirect_stdout(output):
                POC.write_snapshot_ingestion_summary(result)
            self.assertIn("Snapshot ingestion contract dry-run: PASS", output.getvalue())
            self.assertIn("prepared records: 0", output.getvalue())
            self.assertIn("empty input behavior: one empty batch", output.getvalue())

    def test_snapshot_ingestion_preparation_uses_existing_classifier_and_record_contract(self) -> None:
        cases = [
            self._ingestion_case(101, "Fordrevne fra Ukraine"),
            self._ingestion_case(102, "Ændring af udlændingeloven om opholdstilladelse"),
            self._ingestion_case(103, "Vejlov om vejvedligeholdelse"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            result = self._run_ingestion_preparation(pathlib.Path(temp), cases)
            ingestion = result["ingestion"]
            self.assertEqual(ingestion["prepared_record_count"], 3)
            self.assertEqual(ingestion["relevance_counts"], {"DIRECT": 1, "POTENTIAL": 1, "IRRELEVANT": 1})
            self.assertEqual(ingestion["complete_candidate_count"], 3)
            self.assertEqual(ingestion["incomplete_candidate_count"], 0)
            self.assertEqual(ingestion["ruleset_version"], "1.1")

    def test_snapshot_ingestion_preparation_batches_more_than_50_records(self) -> None:
        cases = [self._ingestion_case(index + 1, f"Vejlov {index + 1}") for index in range(51)]
        with tempfile.TemporaryDirectory() as temp:
            result = self._run_ingestion_preparation(pathlib.Path(temp), cases)
            ingestion = result["ingestion"]
            self.assertEqual(ingestion["prepared_record_count"], 51)
            self.assertEqual(ingestion["batch_count"], 2)
            self.assertEqual(ingestion["largest_batch_record_count"], 50)

    def test_snapshot_ingestion_preparation_obeys_batch_byte_limit(self) -> None:
        cases = [self._ingestion_case(index + 1, "x" * 60000) for index in range(2)]
        with tempfile.TemporaryDirectory() as temp:
            result = self._run_ingestion_preparation(pathlib.Path(temp), cases)
            ingestion = result["ingestion"]
            self.assertEqual(ingestion["prepared_record_count"], 2)
            self.assertEqual(ingestion["batch_count"], 2)
            self.assertLessEqual(ingestion["largest_batch_encoded_bytes"], 220 * 1024)

    def test_snapshot_ingestion_preparation_rejects_malformed_case_safely(self) -> None:
        case = self._ingestion_case(404, "PRIVATE TITLE")
        case["source_url"] = None
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            with self.assertRaises(POC.PocError) as raised:
                self._run_ingestion_preparation(root, [case])
            self.assertEqual(raised.exception.category, "snapshot_ingestion_preparation_failed")
            self.assertNotIn("PRIVATE TITLE", str(raised.exception))
            self.assertEqual(list(root.glob("denmarkbot-candidate-bundle-*.json")), [])

    def test_snapshot_ingestion_incomplete_extraction_stays_incomplete_and_never_eligible(self) -> None:
        diagnostics = [{"code": "source_lookup_failed", "count": 1}]
        cases = [self._ingestion_case(505, "PRIVATE TITLE", complete=False)]
        with tempfile.TemporaryDirectory() as temp:
            result = self._run_ingestion_preparation(
                pathlib.Path(temp), cases, complete=False, diagnostics=diagnostics
            )
            ingestion = result["ingestion"]
            self.assertFalse(ingestion["complete"])
            self.assertFalse(ingestion["extraction_complete"])
            self.assertFalse(ingestion["submission_eligible"])
            self.assertEqual(ingestion["notification_blocked_count"], 1)
            self.assertEqual(ingestion["prepared_record_count"], 1)
            self.assertEqual(ingestion["complete_candidate_count"], 0)
            self.assertEqual(ingestion["incomplete_candidate_count"], 1)
            self.assertEqual(ingestion["extraction_diagnostics"], diagnostics)
            output = StringIO()
            with redirect_stdout(output):
                POC.write_snapshot_ingestion_summary(result)
            self.assertIn("Snapshot ingestion contract dry-run: INCOMPLETE", output.getvalue())
            self.assertIn("source_lookup_failed: 1", output.getvalue())
            self.assertNotIn("PRIVATE TITLE", output.getvalue())

    def test_snapshot_ingestion_path_cannot_call_network_upload_or_checkpoint_helpers(self) -> None:
        from collectors import ingestion_client

        cases = [self._ingestion_case(606, "Vejlov")]
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(ingestion_client, "upload", side_effect=AssertionError("upload forbidden")) as upload, \
                mock.patch.object(ingestion_client, "collection_complete", side_effect=AssertionError("checkpoint path forbidden")) as complete, \
                mock.patch.object(ingestion_client, "proposed_checkpoints", side_effect=AssertionError("checkpoint path forbidden")) as checkpoints, \
                mock.patch.object(ingestion_client, "_request", side_effect=AssertionError("network forbidden")) as request:
            self._run_ingestion_preparation(pathlib.Path(temp), cases)
            upload.assert_not_called()
            complete.assert_not_called()
            checkpoints.assert_not_called()
            request.assert_not_called()

    def test_snapshot_ingestion_summary_never_leaks_record_fields(self) -> None:
        case = self._ingestion_case(777, "PRIVATE SNAPSHOT TITLE")
        with tempfile.TemporaryDirectory() as temp:
            result = self._run_ingestion_preparation(pathlib.Path(temp), [case])
            output = StringIO()
            with redirect_stdout(output):
                POC.write_snapshot_ingestion_summary(result)
            rendered = output.getvalue()
            for private in ("777", "PRIVATE SNAPSHOT TITLE", "https://oda.ft.dk", "normalized_snapshot", "payload"):
                self.assertNotIn(private, rendered)
            self.assertIn("submission eligible: false", rendered)

    def test_snapshot_ingestion_candidate_temp_file_is_private_and_cleaned(self) -> None:
        case = self._ingestion_case(808, "Vejlov")
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            observed_modes = []
            original = POC._write_private_candidate_bundle

            def inspect_file(payload, directory):
                path = original(payload, directory)
                observed_modes.append(path.stat().st_mode & 0o777)
                return path

            with mock.patch.object(POC, "_write_private_candidate_bundle", side_effect=inspect_file):
                self._run_ingestion_preparation(root, [case])
            self.assertEqual(observed_modes, [0o600])
            self.assertEqual(list(root.glob("denmarkbot-candidate-bundle-*.json")), [])

    def test_snapshot_client_dry_run_reuses_real_client_and_has_network_hard_guard(self) -> None:
        from collectors import ingestion_client, snapshot_ingestion_client

        case = self._ingestion_case(901, "Forslag om ophold for fordrevne fra Ukraine")
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            with mock.patch.object(snapshot_ingestion_client, "_post", side_effect=AssertionError("HTTP forbidden")) as post, \
                    mock.patch.object(snapshot_ingestion_client.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")) as urlopen, \
                    mock.patch.object(ingestion_client, "upload", side_effect=AssertionError("old upload forbidden")) as upload, \
                    mock.patch.object(ingestion_client, "_request", side_effect=AssertionError("old HTTP helper forbidden")) as request, \
                    mock.patch.object(ingestion_client, "proposed_checkpoints", side_effect=AssertionError("checkpoint helper forbidden")) as checkpoints, \
                    mock.patch.object(ingestion_client, "collection_complete", side_effect=AssertionError("checkpoint helper forbidden")) as collection_complete, \
                    mock.patch.object(snapshot_ingestion_client, "ingest_snapshot_candidates",
                                      wraps=snapshot_ingestion_client.ingest_snapshot_candidates) as client:
                result = self._run_snapshot_client_dry_run(root, [case])
            post.assert_not_called()
            urlopen.assert_not_called()
            upload.assert_not_called()
            request.assert_not_called()
            checkpoints.assert_not_called()
            collection_complete.assert_not_called()
            client.assert_called_once()
            self.assertIs(client.call_args.kwargs["dry_run"], True)
            self.assertEqual(client.call_args.kwargs["mode"], "incremental")
            ingestion = result["snapshot_ingestion"]
            self.assertTrue(ingestion["complete"])
            self.assertTrue(ingestion["collection_complete"])
            self.assertTrue(ingestion["submission_eligible"])
            self.assertEqual(ingestion["candidate_case_count"], 1)
            self.assertEqual(ingestion["prepared_record_count"], 1)
            self.assertEqual(ingestion["batch_count"], 1)
            self.assertEqual(ingestion["base_finish"], "2026-10-18 02:00:00.000")
            self.assertEqual(ingestion["target_finish"], "2026-10-19 02:00:00.000")
            self.assertTrue(ingestion["schema_equal"])
            self.assertEqual(list(root.glob("denmarkbot-candidate-bundle-*.json")), [])

    def test_snapshot_client_zero_candidate_newer_backup_is_valid(self) -> None:
        from collectors import snapshot_ingestion_client

        with tempfile.TemporaryDirectory() as temp:
            result = self._run_snapshot_client_dry_run(pathlib.Path(temp), [])
        ingestion = result["snapshot_ingestion"]
        self.assertEqual((ingestion["candidate_case_count"], ingestion["prepared_record_count"], ingestion["batch_count"]), (0, 0, 1))
        self.assertTrue(ingestion["complete"])
        self.assertTrue(ingestion["submission_eligible"])
        self.assertEqual(ingestion["relevance_counts"], {"DIRECT": 0, "POTENTIAL": 0, "IRRELEVANT": 0})

    def test_snapshot_client_is_not_invoked_for_incomplete_extraction(self) -> None:
        from collectors import snapshot_ingestion_client

        diagnostics = [{"code": "source_lookup_failed", "count": 2}]
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(snapshot_ingestion_client, "ingest_snapshot_candidates", side_effect=AssertionError("incomplete bundle must not reach client")) as client:
            result = self._run_snapshot_client_dry_run(
                pathlib.Path(temp), [self._ingestion_case(902, "PRIVATE TITLE", complete=False)],
                complete=False, diagnostics=diagnostics,
            )
        client.assert_not_called()
        ingestion = result["snapshot_ingestion"]
        self.assertFalse(ingestion["complete"])
        self.assertFalse(ingestion["collection_complete"])
        self.assertFalse(ingestion["submission_eligible"])
        self.assertEqual(ingestion["prepared_record_count"], 0)
        self.assertEqual(ingestion["extraction_diagnostics"], diagnostics)
        output = StringIO()
        with redirect_stdout(output):
            POC.write_snapshot_ingestion_client_summary(result)
        self.assertIn("Snapshot ingestion client dry-run: INCOMPLETE", output.getvalue())
        self.assertIn("source_lookup_failed: 2", output.getvalue())
        self.assertNotIn("PRIVATE TITLE", output.getvalue())

    def test_snapshot_client_summary_is_aggregate_only(self) -> None:
        case = self._ingestion_case(903, "PRIVATE SNAPSHOT TITLE")
        with tempfile.TemporaryDirectory() as temp:
            result = self._run_snapshot_client_dry_run(pathlib.Path(temp), [case])
        output = StringIO()
        with redirect_stdout(output):
            POC.write_snapshot_ingestion_client_summary(result)
        rendered = output.getvalue()
        for private in ("903", "PRIVATE SNAPSHOT TITLE", "https://oda.ft.dk", "backup_sha256", "prepared payload"):
            self.assertNotIn(private, rendered)
        self.assertIn("Snapshot ingestion client dry-run: PASS", rendered)

    def test_snapshot_client_preflight_requires_progression_and_same_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            old, new = root / "old.sqlite", root / "new.sqlite"
            create_candidate_manifest(old, backup="1", finish="2026-10-18 02:00:00.000", include_sag=False)
            create_candidate_manifest(new, backup="1", finish="2026-10-18 02:00:00.000", include_sag=False)
            with self.assertRaises(POC.PocError) as same:
                POC.validate_candidate_manifest_inputs(old, new, "100", "101", require_newer_snapshot=True)
            self.assertEqual(same.exception.category, "snapshot_dry_run_target_not_newer")
            create_candidate_manifest(new, backup="2", finish="2026-10-17 02:00:00.000", include_sag=False)
            with self.assertRaises(POC.PocError) as older:
                POC.validate_candidate_manifest_inputs(old, new, "100", "101", require_newer_snapshot=True)
            self.assertEqual(older.exception.category, "snapshot_dry_run_regression")
            create_candidate_manifest(new, backup="2", finish="2026-10-19 02:00:00.000", include_sag=False,
                                      schema_fingerprint="d" * 64)
            with self.assertRaises(POC.PocError) as schema:
                POC.validate_candidate_manifest_inputs(old, new, "100", "101", require_newer_snapshot=True)
            self.assertEqual(schema.exception.category, "snapshot_dry_run_schema_changed")

    def test_snapshot_client_mode_and_existing_manual_modes_remain(self) -> None:
        self.assertEqual(POC.validate_mode("SNAPSHOT_INGESTION_DRY_RUN"), "SNAPSHOT_INGESTION_DRY_RUN")
        self.assertEqual(POC.validate_mode("SNAPSHOT_INCREMENTAL_DRY_RUN"), "SNAPSHOT_INCREMENTAL_DRY_RUN")
        for mode in ("RESTORE_VALIDATE", "VALIDATE_SQL_PROVIDER", "CREATE_MANIFEST_BASELINE",
                     "EXTRACT_SNAPSHOT_CANDIDATES", "CLASSIFY_SNAPSHOT_CANDIDATES",
                     "PREPARE_SNAPSHOT_INGESTION", "HISTORICAL_SNAPSHOT_BASELINE_LIVE"):
            self.assertEqual(POC.validate_mode(mode), mode)
        workflow = (ROOT / ".github/workflows/folketinget-backup-poc.yml").read_text()
        self.assertIn("- DIFF_MANIFESTS", workflow)
        self.assertIn("- SNAPSHOT_INGESTION_DRY_RUN", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertEqual(workflow.count("uses: actions/download-artifact@v8"), 4)
        self.assertNotIn("actions/download-artifact@v4", workflow)

    def test_historical_baseline_mode_validates_one_exact_manifest_artifact(self) -> None:
        self.assertEqual(POC.validate_mode("HISTORICAL_SNAPSHOT_BASELINE_DRY_RUN"),
                         "HISTORICAL_SNAPSHOT_BASELINE_DRY_RUN")
        with tempfile.TemporaryDirectory() as temp:
            directory = pathlib.Path(temp) / "folketinget-manifest-12345"
            directory.mkdir()
            create_candidate_manifest(directory / "manifest.sqlite", backup="a", finish="2026-10-19T02:00:00Z", include_sag=False)
            context = POC.validate_historical_manifest_input(directory, "12345")
            self.assertEqual(context["path"], directory.resolve() / "manifest.sqlite")
            self.assertEqual(context["report"]["new"]["backup_sha256"], "a" * 64)
            with self.assertRaises(POC.PocError) as invalid_id:
                POC.validate_historical_manifest_input(directory, "12x")
            self.assertEqual(invalid_id.exception.category, "workflow_run_id_invalid")
            (directory / "extra.txt").write_text("unexpected")
            with self.assertRaises(POC.PocError) as ambiguous:
                POC.validate_historical_manifest_input(directory, "12345")
            self.assertEqual(ambiguous.exception.category, "manifest_artifact_invalid")

    def test_historical_mode_requires_exact_restored_backup_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = pathlib.Path(temp) / "folketinget-manifest-77"
            directory.mkdir()
            create_candidate_manifest(directory / "manifest.sqlite", backup="a", finish="2026-10-19T02:00:00Z", include_sag=False)
            context = POC.validate_historical_manifest_input(directory, "77")
            POC.validate_candidate_backup_identity("a" * 64, "2026-10-19T02:00:00Z", "a" * 64, context)
            for checksum, finish, fingerprint, category in (
                ("b" * 64, "2026-10-19T02:00:00Z", "a" * 64, "candidate_backup_sha_mismatch"),
                ("a" * 64, "2026-10-20T02:00:00Z", "a" * 64, "candidate_backup_finish_mismatch"),
                ("a" * 64, "2026-10-19T02:00:00Z", "b" * 64, "candidate_schema_mismatch"),
            ):
                with self.subTest(category=category), self.assertRaises(POC.PocError) as raised:
                    POC.validate_candidate_backup_identity(checksum, finish, fingerprint, context)
                self.assertEqual(raised.exception.category, category)

    def test_historical_dry_run_reuses_streaming_module_and_never_calls_submission_helpers(self) -> None:
        from collectors import ingestion_client, snapshot_ingestion_client
        import folketinget_historical_baseline as historical_baseline

        target = {"backup_sha256": "a" * 64, "backup_finish_date": "2026-10-18 02:00:00.000", "schema_fingerprint": "b" * 64}
        context = {"path": pathlib.Path("private-manifest.sqlite"), "report": {"new": target}}
        record = ingestion_client.prepare_records(
            {"cases": [{"source": "folketinget", "external_id": "42", "title": "Public title",
                        "source_url": "https://oda.ft.dk/api/Sag(42)", "complete": True,
                        "number": "L 42", "status": {}, "documents": [], "stages": [], "votes": []}]},
            source="folketinget", mode="historical",
        )[0]
        record["relevance"] = "DIRECT"
        expected = {"status": "PASS", "manifest_sag_count": 1, "target_backup_finish": target["backup_finish_date"],
                    "selected_historical_ingestion_record_count": 1,
                    "selected_historical_ingestion_batch_count": 1, "DIRECT": 1, "POTENTIAL": 0}
        with mock.patch.object(historical_baseline, "run_historical_baseline_with_records", return_value=(expected, [record])) as run, \
                mock.patch.object(ingestion_client, "upload", side_effect=AssertionError("forbidden")), \
                mock.patch.object(ingestion_client, "_request", side_effect=AssertionError("forbidden")), \
                mock.patch.object(ingestion_client, "proposed_checkpoints", side_effect=AssertionError("forbidden")), \
                mock.patch.object(ingestion_client, "collection_complete", side_effect=AssertionError("forbidden")), \
                mock.patch.object(snapshot_ingestion_client, "_post", side_effect=AssertionError("forbidden")):
            result = POC.run_historical_baseline_dry_run(context, SimpleNamespace(), progress_callback=lambda *_: None)
        self.assertEqual(result["snapshot_client_dry_run"]["status"], "PASS")
        self.assertTrue(result["snapshot_client_dry_run"]["submission_eligible"])
        run.assert_called_once_with(context["path"], mock.ANY, progress_callback=mock.ANY)

    def test_historical_live_reuses_full_proof_and_exact_prepared_records(self) -> None:
        from collectors import ingestion_client, snapshot_ingestion_client
        import folketinget_historical_baseline as historical_baseline

        target = {"backup_sha256": "a" * 64, "backup_finish_date": "2026-10-18 02:00:00.000", "schema_fingerprint": "b" * 64}
        context = {"path": pathlib.Path("private-manifest.sqlite"), "report": {"new": target}}
        records = ingestion_client.prepare_records(
            {"cases": [
                {"external_id": str(case_id), "title": "PRIVATE TITLE", "source_url": f"https://oda.ft.dk/api/Sag({case_id})",
                 "complete": True, "number": f"L {case_id}", "status": {}, "documents": [], "stages": [], "votes": []}
                for case_id in (42, 43)
            ]}, "folketinget", "historical",
        )
        for record in records:
            record.update({"relevance": "DIRECT", "historical": True})
        baseline = {"status": "PASS", "manifest_sag_count": 1, "target_backup_finish": target["backup_finish_date"],
                    "selected_historical_ingestion_record_count": 2,
                    "selected_historical_ingestion_batch_count": 1, "DIRECT": 2, "POTENTIAL": 0}
        committed = {
            "status": "COMMITTED", "mode": "historical", "state_kind": "snapshot",
            "run_id": POC.workflow_historical_run_id("39410000"), "records": 2,
            "accepted_records": 2, "processed_records": 1, "batches": 1,
            "relevance_counts": {"DIRECT": 2, "POTENTIAL": 0}, "base_snapshot": None,
            "target_snapshot": target, "snapshot_generation": 1, "snapshot_advanced": True,
            "start_idempotent": True, "commit_resumed": True,
        }
        with mock.patch.object(historical_baseline, "run_historical_baseline_with_records", return_value=(baseline, records)) as run, \
                mock.patch.object(snapshot_ingestion_client, "submit_prepared_historical_records", return_value=committed) as submit:
            result = POC.run_historical_baseline_live(
                context, SimpleNamespace(), endpoint="https://example.test", token="NO_LOG_TOKEN",
                workflow_run_id="39410000", progress_callback=lambda *_: None,
            )
        self.assertEqual(result["snapshot_client_live"]["status"], "PASS")
        self.assertEqual(result["snapshot_client_live"]["final_ingestion_status"], "COMMITTED")
        self.assertEqual(POC.historical_baseline_live_exit_code(result), 0)
        run.assert_called_once_with(context["path"], mock.ANY, progress_callback=mock.ANY)
        submitted_records = submit.call_args.args[0]
        self.assertIs(submitted_records[0], records[0])
        self.assertEqual(submit.call_args.kwargs["base_snapshot"], None)
        self.assertEqual(submit.call_args.kwargs["target_snapshot"], target)
        self.assertEqual(submit.call_args.kwargs["activation_mode"], POC.HISTORICAL_SNAPSHOT_BASELINE_LIVE)
        self.assertEqual(submit.call_args.kwargs["run_id"], committed["run_id"])

    def test_historical_live_incomplete_or_manifest_mismatch_never_submits(self) -> None:
        from collectors import snapshot_ingestion_client
        import folketinget_historical_baseline as historical_baseline

        target = {"backup_sha256": "a" * 64, "backup_finish_date": "2026-10-18 02:00:00.000", "schema_fingerprint": "b" * 64}
        context = {"path": pathlib.Path("private-manifest.sqlite"), "report": {"new": target}}
        incomplete = {"status": "INCOMPLETE", "diagnostics": {}, "target_backup_finish": target["backup_finish_date"]}
        with mock.patch.object(historical_baseline, "run_historical_baseline_with_records", return_value=(incomplete, [])), \
                mock.patch.object(snapshot_ingestion_client, "submit_prepared_historical_records", side_effect=AssertionError("must not submit")) as submit:
            result = POC.run_historical_baseline_live(
                context, SimpleNamespace(), endpoint="https://example.test", token="token", workflow_run_id="39410001"
            )
        submit.assert_not_called()
        self.assertEqual(result["snapshot_client_live"]["final_ingestion_status"], "NOT_STARTED")
        self.assertEqual(POC.historical_baseline_live_exit_code(result), 2)

        mismatch = {"status": "PASS", "target_backup_finish": "2026-10-17 02:00:00.000",
                    "selected_historical_ingestion_record_count": 0,
                    "selected_historical_ingestion_batch_count": 1, "DIRECT": 0, "POTENTIAL": 0}
        with mock.patch.object(historical_baseline, "run_historical_baseline_with_records", return_value=(mismatch, [])), \
                mock.patch.object(snapshot_ingestion_client, "submit_prepared_historical_records", side_effect=AssertionError("must not submit")) as submit:
            with self.assertRaises(POC.PocError) as raised:
                POC.run_historical_baseline_live(
                    context, SimpleNamespace(), endpoint="https://example.test", token="token", workflow_run_id="39410002"
                )
        self.assertEqual(raised.exception.category, "historical_handoff_snapshot_mismatch")
        submit.assert_not_called()

    def test_historical_live_summary_is_aggregate_only_and_run_id_is_stable(self) -> None:
        first = POC.workflow_historical_run_id("39410003")
        self.assertEqual(first, POC.workflow_historical_run_id("39410003"))
        summary = {
            "status": "PASS", "manifest_sag_count": 99682,
            "selected_historical_ingestion_record_count": 1265,
            "selected_historical_ingestion_batch_count": 26,
            "DIRECT": 45, "POTENTIAL": 1220,
            "target_backup_finish": "2026-10-18 02:00:00.000",
            "snapshot_client_live": {"status": "PASS", "run_id": first,
                "final_ingestion_status": "COMMITTED", "snapshot_generation": 1,
                "snapshot_advanced": True},
        }
        output = StringIO()
        with redirect_stdout(output):
            POC.write_historical_baseline_live_summary(summary)
        rendered = output.getvalue()
        self.assertIn("Historical snapshot baseline live submission: PASS", rendered)
        self.assertIn("full Sag count: 99682", rendered)
        self.assertIn("selected record count: 1265", rendered)
        self.assertIn("batch count: 26", rendered)
        self.assertIn("final ingestion status: COMMITTED", rendered)
        self.assertNotIn("PRIVATE TITLE", rendered)
        self.assertNotIn("https://", rendered)
        self.assertNotIn("records", rendered.lower().split("run_id:")[0])

    def test_historical_handoff_requires_full_pass_and_exact_client_counts_and_identity(self) -> None:
        from collectors import ingestion_client, snapshot_ingestion_client
        import folketinget_historical_baseline as historical_baseline

        target = {"backup_sha256": "a" * 64, "backup_finish_date": "2026-10-18 02:00:00.000", "schema_fingerprint": "b" * 64}
        context = {"path": pathlib.Path("private-manifest.sqlite"), "report": {"new": target}}
        incomplete = {"status": "INCOMPLETE", "manifest_sag_count": 1, "diagnostics": {}}
        with mock.patch.object(historical_baseline, "run_historical_baseline_with_records", return_value=(incomplete, [])), \
                mock.patch.object(snapshot_ingestion_client, "dry_run_prepared_historical_records", side_effect=AssertionError("must not hand off")) as client:
            result = POC.run_historical_baseline_dry_run(context, SimpleNamespace())
        client.assert_not_called()
        self.assertEqual(result["snapshot_client_dry_run"]["status"], "FAIL")
        self.assertEqual(POC.historical_baseline_exit_code(result), 2)

        record = ingestion_client.prepare_records(
            {"cases": [{"external_id": "42", "title": "Ukraine law", "source_url": "https://oda.ft.dk/api/Sag(42)",
                        "complete": True, "number": "L 42", "status": {}, "documents": [], "stages": [], "votes": []}]},
            "folketinget", "historical",
        )[0]
        record["relevance"] = "DIRECT"
        complete = {"status": "PASS", "target_backup_finish": target["backup_finish_date"],
                    "selected_historical_ingestion_record_count": 1, "selected_historical_ingestion_batch_count": 1,
                    "DIRECT": 1, "POTENTIAL": 0, "diagnostics": {}}
        wrong_client = {"dry_run": True, "mode": "historical", "state_kind": "snapshot", "records": 0,
                        "batches": 1, "relevance_counts": {"DIRECT": 0, "POTENTIAL": 0, "IRRELEVANT": 0},
                        "collection_complete": True, "submission_eligible": True, "base_snapshot": None,
                        "target_snapshot": target}
        with mock.patch.object(historical_baseline, "run_historical_baseline_with_records", return_value=(complete, [record])), \
                mock.patch.object(snapshot_ingestion_client, "dry_run_prepared_historical_records", return_value=wrong_client):
            result = POC.run_historical_baseline_dry_run(context, SimpleNamespace())
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertFalse(result["snapshot_client_dry_run"]["submission_eligible"])
        self.assertEqual(result["diagnostics"]["snapshot_client_contract_mismatch"], 1)
        self.assertEqual(POC.historical_baseline_exit_code(result), 2)

    def test_historical_baseline_summary_is_aggregate_and_incomplete_is_explicit(self) -> None:
        summary = {
            "status": "INCOMPLETE", "manifest_sag_count": 3, "hydrated_case_count": 2,
            "prepared_record_count": 2, "DIRECT": 1, "POTENTIAL": 0, "IRRELEVANT": 1,
            "notification_blocked_count": 1, "full_universe_complete": False,
            "selected_historical_ingestion_record_count": 1,
            "selected_historical_ingestion_batch_count": 1,
            "selected_largest_batch_records": 1, "selected_largest_batch_bytes": 500,
            "selected_payload_fits_worker_limit": True, "incomplete_case_count": 1,
            "provider_adaptive_splits": 4, "provider_adaptive_split_successes": 0,
            "provider_max_split_depth": 2, "provider_terminal_singleton_failures": 2,
            "provider_terminal_transport_row_shapes": {"null_token": 1, "missing_separator_ascii": 1},
            "provider_terminal_transport_line_length_buckets": {"1-16": 1, "17-64": 1},
            "diagnostics": {
                "missing_status": 1,
                "source_lookup_failed_SagDokument_by_field_sagid_provider_transport_failed_transport_manifest_page_json_invalid": 120,
            }, "target_backup_finish": "2026-10-19T02:00:00Z",
            "ruleset_version": "1.1", "private_title": "DO NOT PRINT", "external_id": "987654",
            "snapshot_client_dry_run": {"status": "FAIL", "records": 1, "batches": 1,
                                         "DIRECT": 1, "POTENTIAL": 0, "submission_eligible": False},
        }
        output = StringIO()
        with redirect_stdout(output):
            POC.write_historical_baseline_summary(summary)
        rendered = output.getvalue()
        self.assertIn("Historical snapshot baseline dry-run: INCOMPLETE", rendered)
        self.assertEqual(POC.historical_baseline_exit_code(summary), 2)
        self.assertIn("missing_status: 1", rendered)
        self.assertIn("full-universe completeness proof: false", rendered)
        self.assertIn("selected historical ingestion records: 1", rendered)
        self.assertIn("selected historical ingestion batches: 1", rendered)
        self.assertIn("selected payload fits Worker 200-batch limit: true", rendered)
        self.assertIn("provider terminal singleton failures: 2", rendered)
        self.assertIn("null_token: 1", rendered)
        self.assertIn("missing_separator_ascii: 1", rendered)
        self.assertIn("1-16: 1", rendered)
        self.assertIn("source_lookup_failed_SagDokument_by_field_sagid_provider_transport_failed_transport_manifest_page_json_invalid: 120", rendered)
        self.assertNotIn("DO NOT PRINT", rendered)
        self.assertNotIn("987654", rendered)
        summary["status"] = "PASS"
        summary["full_universe_complete"] = True
        summary["selected_payload_fits_worker_limit"] = True
        summary["snapshot_client_dry_run"] = {"status": "PASS", "records": 1, "batches": 1,
                                              "DIRECT": 1, "POTENTIAL": 0, "submission_eligible": True}
        output = StringIO()
        with redirect_stdout(output):
            POC.write_historical_baseline_summary(summary)
        self.assertIn("Historical snapshot baseline dry-run: PASS", output.getvalue())
        self.assertIn("snapshot client dry-run: PASS", output.getvalue())
        self.assertEqual(POC.historical_baseline_exit_code(summary), 0)

    def test_historical_progress_reports_only_aggregate_counts_at_bounded_intervals(self) -> None:
        reporter = POC.historical_baseline_progress_reporter()
        output = StringIO()
        with redirect_stdout(output):
            reporter(4999, 10000)
            reporter(5000, 10000)
            reporter(5250, 10000)
            reporter(10000, 10000)
        self.assertEqual(output.getvalue().splitlines(), [
            "progress=historical_baseline processed=5000 manifest_total=10000",
            "progress=historical_baseline processed=10000 manifest_total=10000",
        ])
        self.assertNotIn("id", output.getvalue())

    def test_historical_workflow_is_manual_single_manifest_and_cleans_up(self) -> None:
        workflow = (ROOT / ".github/workflows/folketinget-backup-poc.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn("- HISTORICAL_SNAPSHOT_BASELINE_DRY_RUN", workflow)
        self.assertIn("manifest_run_id:", workflow)
        self.assertIn("inputs.mode == 'HISTORICAL_SNAPSHOT_BASELINE_DRY_RUN'", workflow)
        self.assertIn("actions/runs/$MANIFEST_RUN_ID", workflow)
        self.assertIn('"$event" != "workflow_dispatch"', workflow)
        self.assertIn('"$actual_workflow_id" != "$workflow_id"', workflow)
        self.assertIn("folketinget-snapshot-manifest-$MANIFEST_RUN_ID", workflow)
        self.assertIn("name: Download historical manifest artifact", workflow)
        self.assertIn("uses: actions/download-artifact@v8", workflow)
        self.assertIn("if: always() && (inputs.mode == 'HISTORICAL_SNAPSHOT_BASELINE_DRY_RUN' || inputs.mode == 'HISTORICAL_SNAPSHOT_BASELINE_LIVE')", workflow)
        self.assertIn("INPUT_MANIFEST_RUN_ID:", workflow)
        self.assertIn("HISTORICAL_MANIFEST_PATH:", workflow)
        self.assertIn("- HISTORICAL_SNAPSHOT_PRODUCTION_PREFLIGHT", workflow)
        self.assertIn("if: inputs.mode == 'HISTORICAL_SNAPSHOT_PRODUCTION_PREFLIGHT'", workflow)
        self.assertIn("INPUT_CONFIRM_RESTORE: ${{ inputs.confirm_restore }}", workflow)
        self.assertIn("INGEST_API_URL: ${{ vars.INGEST_API_URL }}", workflow)
        self.assertIn("INGEST_API_TOKEN: ${{ secrets.INGEST_API_TOKEN }}", workflow)
        self.assertIn("/internal/snapshot-readiness?source=folketinget", workflow)
        self.assertIn("- HISTORICAL_SNAPSHOT_BASELINE_LIVE", workflow)
        self.assertIn("INGEST_API_URL: ${{ inputs.mode == 'HISTORICAL_SNAPSHOT_BASELINE_LIVE' && vars.INGEST_API_URL || '' }}", workflow)
        self.assertIn("INGEST_API_TOKEN: ${{ inputs.mode == 'HISTORICAL_SNAPSHOT_BASELINE_LIVE' && secrets.INGEST_API_TOKEN || '' }}", workflow)
        self.assertIn("if: inputs.mode == 'HISTORICAL_SNAPSHOT_BASELINE_DRY_RUN' || inputs.mode == 'HISTORICAL_SNAPSHOT_BASELINE_LIVE'", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("notification-queue", workflow)
        preflight = workflow.split("name: Production snapshot readiness preflight", 1)[1].split("name: Validate manifest run IDs", 1)[0]
        self.assertIn('"User-Agent": "DenmarkTgBot-SnapshotReadinessPreflight/1.0"', preflight)
        self.assertIn('"Authorization": "Bearer " + token', preflight)
        self.assertIn('"Accept": "application/json"', preflight)
        self.assertIn("class NoRedirect", preflight)
        self.assertIn("response.read(4097)", preflight)
        self.assertIn("len(raw_body) > 4096", preflight)
        http_error_handler = preflight.index("except urllib.error.HTTPError as error:")
        url_error_handler = preflight.index("except (urllib.error.URLError, TimeoutError, OSError")
        self.assertLess(http_error_handler, url_error_handler)
        for category in (
            "worker_unauthorized", "worker_forbidden", "readiness_endpoint_not_found",
            "readiness_method_not_allowed", "worker_server_error", "worker_http_error",
        ):
            self.assertIn(category, preflight)
        self.assertIn("finish(\"FAIL\", category, http_status=http_status)", preflight)
        self.assertIn("finish(\"FAIL\", \"worker_request_failed\")", preflight)
        self.assertNotIn("error.read()", preflight)
        self.assertIn("inputs.mode != 'HISTORICAL_SNAPSHOT_PRODUCTION_PREFLIGHT'", workflow)
        self.assertNotIn("actions/download-artifact@v8", preflight)
        self.assertNotIn("curl", preflight)
        self.assertNotIn("sendMessage", workflow)
        self.assertNotIn("snapshot-ingestion-client", workflow)
        self.assertIn("actions/download-artifact@v8", workflow)

    def test_candidate_orchestration_handles_zero_diff_added_sag_and_changed_child(self) -> None:
        from folketinget_candidate_extraction import InMemoryFolketingetProvider

        stamp = "2026-10-19T02:00:00Z"
        sag_row = {
            "id": 42, "nummer": "L 42", "titel": "PRIVATE TITLE", "typeid": None,
            "kategoriid": None, "statusid": None, "opdateringsdato": stamp,
            "retsinformationsurl": None,
        }
        stage_row = {
            "id": 57, "sagid": 42, "titel": "Private stage", "dato": None,
            "typeid": None, "statusid": None, "opdateringsdato": stamp,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            old, new = root / "old.sqlite", root / "new.sqlite"
            create_candidate_manifest(old, backup="1", finish=stamp, include_sag=False)
            create_candidate_manifest(new, backup="1", finish=stamp, include_sag=False)
            zero = POC.run_candidate_extraction(
                old, new, InMemoryFolketingetProvider({}), root, stamp, stamp
            )
            self.assertTrue(zero["complete"])
            self.assertEqual(zero["candidate_case_count"], 0)
            self.assertTrue(all(value == 0 for value in zero["diff_totals"].values()))

            # Distinct run IDs may reference the same source snapshot; this must be a valid zero diff.
            zero_context = POC.validate_candidate_manifest_inputs(old, new, "39225908640", "39231934823")
            self.assertEqual(zero_context["report"]["totals"]["added"], 0)

            create_candidate_manifest(old, backup="1", finish="2026-10-18T02:00:00Z", include_sag=False)
            create_candidate_manifest(new, backup="2", finish=stamp, include_sag=True)
            added = POC.run_candidate_extraction(
                old, new, InMemoryFolketingetProvider({"Sag": [sag_row]}), root,
                "2026-10-18T02:00:00Z", stamp,
            )
            self.assertTrue(added["complete"])
            self.assertEqual((added["affected_case_count"], added["hydrated_case_count"], added["candidate_case_count"]), (1, 1, 1))
            output = StringIO()
            with redirect_stdout(output):
                POC.write_candidate_extraction_summary(added)
            self.assertIn("added: 1", output.getvalue())
            self.assertNotIn("PRIVATE TITLE", output.getvalue())
            self.assertNotIn("42", output.getvalue())

            key = json.dumps({"entity": "Sagstrin", "key": [["id", "int", 57]]}, sort_keys=True, separators=(",", ":"))
            create_candidate_manifest(old, backup="1", finish="2026-10-18T02:00:00Z", include_sag=False)
            create_candidate_manifest(
                new, backup="2", finish=stamp, include_sag=False,
                extra_records={"Sagstrin": [(key, b"f" * 32, b"s" * 32)]},
            )
            child = POC.run_candidate_extraction(
                old, new, InMemoryFolketingetProvider({"Sag": [sag_row], "Sagstrin": [stage_row]}), root,
                "2026-10-18T02:00:00Z", stamp,
            )
            self.assertTrue(child["complete"])
            self.assertEqual(child["candidate_case_count"], 1)

    def test_snapshot_incremental_same_identity_is_noop(self) -> None:
        stamp = "2026-10-20 02:00:40.000"
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            base_dir = root / "folketinget-base-manifest-39433055408"
            base_dir.mkdir()
            base = base_dir / "manifest.sqlite"
            target = root / "current.sqlite"
            rows = {entity: [] for entity in POC.REQUIRED_ENTITIES}
            rows["Sag"] = [(42, b"f" * 32, b"s" * 32, {"statusid": None})]
            create_v2_candidate_manifest(base, backup="a", finish=stamp, rows=rows)
            create_v2_candidate_manifest(target, backup="a", finish=stamp, rows=rows)
            context = POC.validate_snapshot_incremental_base_input(base_dir, "39433055408")
            with mock.patch.object(POC, "run_candidate_extraction") as extract:
                result = POC.run_snapshot_incremental_dry_run(context, target, SimpleNamespace(), root)
            extract.assert_not_called()
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["snapshot_change"], "SAME_SNAPSHOT")
            self.assertEqual(result["diff_totals"]["unchanged"], 1)
            self.assertEqual(result["prepared_record_count"], 0)
            self.assertEqual(result["snapshot_client_dry_run"], "PASS")
            self.assertFalse(result["submission_eligible"])

    def test_snapshot_incremental_changed_snapshot_uses_exact_identities_and_dry_run_only(self) -> None:
        from folketinget_candidate_extraction import InMemoryFolketingetProvider
        from collectors import snapshot_ingestion_client

        old_finish, new_finish = "2026-10-20 02:00:40.000", "2026-10-21 02:00:40.000"
        sag = {
            "id": 42, "nummer": "L 42", "titel": "Private test title", "typeid": None,
            "kategoriid": None, "statusid": None, "opdateringsdato": "2026-10-21T02:00:40Z",
            "retsinformationsurl": None,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            base_dir = root / "folketinget-base-manifest-39433055408"
            base_dir.mkdir()
            base, target = base_dir / "manifest.sqlite", root / "current.sqlite"
            old_rows = {entity: [] for entity in POC.REQUIRED_ENTITIES}
            new_rows = {entity: [] for entity in POC.REQUIRED_ENTITIES}
            old_rows["Sag"] = [(42, b"f" * 32, b"s" * 32, {"statusid": None})]
            new_rows["Sag"] = [(42, b"g" * 32, b"t" * 32, {"statusid": None})]
            create_v2_candidate_manifest(base, backup="a", finish=old_finish, rows=old_rows)
            create_v2_candidate_manifest(target, backup="b", finish=new_finish, rows=new_rows)
            context = POC.validate_snapshot_incremental_base_input(base_dir, "39433055408")
            original = snapshot_ingestion_client.ingest_snapshot_candidates
            with mock.patch.object(snapshot_ingestion_client, "_submit_prepared_records", side_effect=AssertionError("live path reached")):
                with mock.patch.object(snapshot_ingestion_client, "ingest_snapshot_candidates", wraps=original) as client:
                    result = POC.run_snapshot_incremental_dry_run(
                        context, target, InMemoryFolketingetProvider({"Sag": [sag]}), root
                    )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["snapshot_change"], "CHANGED")
            self.assertEqual(result["diff_totals"]["changed"], 1)
            self.assertEqual(result["affected_case_count"], 1)
            self.assertEqual(result["hydrated_case_count"], 1)
            kwargs = client.call_args.kwargs
            self.assertIs(kwargs["dry_run"], True)
            self.assertEqual(kwargs["base_snapshot"], context["identity"])
            self.assertEqual(kwargs["target_snapshot"], {
                "backup_sha256": "b" * 64, "backup_finish_date": new_finish,
                "schema_fingerprint": "a" * 64,
            })
            self.assertTrue(result["submission_eligible"])

    def test_snapshot_incremental_removed_v2_relation_routes_through_full_dry_run(self) -> None:
        from folketinget_candidate_extraction import InMemoryFolketingetProvider

        old_finish, new_finish = "2026-10-20 02:00:40.000", "2026-10-21 02:00:40.000"
        sag = {
            "id": 42, "nummer": "L 42", "titel": "Private test title", "typeid": None,
            "kategoriid": None, "statusid": None, "opdateringsdato": "2026-10-21T02:00:40Z",
            "retsinformationsurl": None,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            base_dir = root / "folketinget-base-manifest-39433055408"
            base_dir.mkdir()
            base, target = base_dir / "manifest.sqlite", root / "current.sqlite"
            old_rows = {entity: [] for entity in POC.REQUIRED_ENTITIES}
            new_rows = {entity: [] for entity in POC.REQUIRED_ENTITIES}
            sag_row = (42, b"f" * 32, b"s" * 32, {"statusid": None})
            old_rows["Sag"] = [sag_row]
            new_rows["Sag"] = [sag_row]
            old_rows["SagDokument"] = [(9, b"l" * 32, b"m" * 32, {"sagid": 42, "dokumentid": 100})]
            create_v2_candidate_manifest(base, backup="a", finish=old_finish, rows=old_rows)
            create_v2_candidate_manifest(target, backup="b", finish=new_finish, rows=new_rows)
            context = POC.validate_snapshot_incremental_base_input(base_dir, "39433055408")
            result = POC.run_snapshot_incremental_dry_run(
                context, target, InMemoryFolketingetProvider({"Sag": [sag]}), root
            )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["diff_totals"]["removed"], 1)
            self.assertEqual((result["affected_case_count"], result["hydrated_case_count"]), (1, 1))
            self.assertEqual(result["snapshot_client_dry_run"], "PASS")

    def test_snapshot_incremental_incomplete_extraction_blocks_client(self) -> None:
        from folketinget_candidate_extraction import InMemoryFolketingetProvider
        from collectors import snapshot_ingestion_client

        old_finish, new_finish = "2026-10-20 02:00:40.000", "2026-10-21 02:00:40.000"
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            base_dir = root / "folketinget-base-manifest-39433055408"
            base_dir.mkdir()
            base, target = base_dir / "manifest.sqlite", root / "current.sqlite"
            old_rows = {entity: [] for entity in POC.REQUIRED_ENTITIES}
            new_rows = {entity: [] for entity in POC.REQUIRED_ENTITIES}
            old_rows["Sag"] = [(42, b"f" * 32, b"s" * 32, {"statusid": None})]
            new_rows["Sag"] = [(42, b"g" * 32, b"t" * 32, {"statusid": None})]
            create_v2_candidate_manifest(base, backup="a", finish=old_finish, rows=old_rows)
            create_v2_candidate_manifest(target, backup="b", finish=new_finish, rows=new_rows)
            context = POC.validate_snapshot_incremental_base_input(base_dir, "39433055408")
            incomplete = self._candidate_bundle([], complete=False, diagnostics=[{"code": "affected_case_missing", "count": 1}])
            incomplete["diff"]["totals"]["changed"] = 1
            incomplete["diff"]["entities"]["Sag"]["changed"] = 1
            with mock.patch("folketinget_candidate_extraction.extract_candidates", return_value=incomplete):
                with mock.patch.object(snapshot_ingestion_client, "ingest_snapshot_candidates") as client:
                    result = POC.run_snapshot_incremental_dry_run(
                        context, target, InMemoryFolketingetProvider({}), root
                    )
            client.assert_not_called()
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertEqual(result["snapshot_client_dry_run"], "FAIL")
            self.assertFalse(result["submission_eligible"])

    def test_snapshot_incremental_removed_sag_remains_fail_closed(self) -> None:
        from folketinget_candidate_extraction import InMemoryFolketingetProvider
        from collectors import snapshot_ingestion_client

        old_finish, new_finish = "2026-10-20 02:00:40.000", "2026-10-21 02:00:40.000"
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            base_dir = root / "folketinget-base-manifest-39433055408"
            base_dir.mkdir()
            base, target = base_dir / "manifest.sqlite", root / "current.sqlite"
            old_rows = {entity: [] for entity in POC.REQUIRED_ENTITIES}
            new_rows = {entity: [] for entity in POC.REQUIRED_ENTITIES}
            old_rows["Sag"] = [(42, b"f" * 32, b"s" * 32, {"statusid": None})]
            create_v2_candidate_manifest(base, backup="a", finish=old_finish, rows=old_rows)
            create_v2_candidate_manifest(target, backup="b", finish=new_finish, rows=new_rows)
            context = POC.validate_snapshot_incremental_base_input(base_dir, "39433055408")
            with mock.patch.object(snapshot_ingestion_client, "ingest_snapshot_candidates") as client:
                result = POC.run_snapshot_incremental_dry_run(
                    context, target, InMemoryFolketingetProvider({}), root
                )
            client.assert_not_called()
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertFalse(result["submission_eligible"])

    def test_snapshot_incremental_rejects_v1_base_and_over_limit_batches(self) -> None:
        from folketinget_candidate_extraction import InMemoryFolketingetProvider

        old_finish, new_finish = "2026-10-20 02:00:40.000", "2026-10-21 02:00:40.000"
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            base_dir = root / "folketinget-base-manifest-39433055408"
            base_dir.mkdir()
            create_candidate_manifest(base_dir / "manifest.sqlite", backup="a", finish=old_finish, include_sag=False)
            with self.assertRaises(POC.PocError) as v1:
                POC.validate_snapshot_incremental_base_input(base_dir, "39433055408")
            self.assertEqual(v1.exception.category, "snapshot_incremental_base_routing_required")

            base, target = base_dir / "manifest.sqlite", root / "current.sqlite"
            old_rows = {entity: [] for entity in POC.REQUIRED_ENTITIES}
            new_rows = {entity: [] for entity in POC.REQUIRED_ENTITIES}
            old_rows["Sag"] = [(42, b"f" * 32, b"s" * 32, {"statusid": None})]
            new_rows["Sag"] = [(42, b"g" * 32, b"t" * 32, {"statusid": None})]
            create_v2_candidate_manifest(base, backup="a", finish=old_finish, rows=old_rows)
            create_v2_candidate_manifest(target, backup="b", finish=new_finish, rows=new_rows)
            context = POC.validate_snapshot_incremental_base_input(base_dir, "39433055408")
            oversized = {
                "complete": True, "diff_totals": {"added": 0, "removed": 0, "changed": 1,
                                                     "technical_only": 0, "unchanged": 0},
                "affected_case_count": 1, "hydrated_case_count": 1,
                "snapshot_ingestion": {
                    "complete": True, "batch_count": 201, "relevance_counts": {},
                    "prepared_record_count": 1, "submission_eligible": True,
                },
            }
            with mock.patch.object(POC, "run_candidate_extraction", return_value=oversized):
                with self.assertRaises(POC.PocError) as limit:
                    POC.run_snapshot_incremental_dry_run(context, target, SimpleNamespace(), root)
            self.assertEqual(limit.exception.category, "snapshot_incremental_batch_limit_exceeded")

    def test_snapshot_incremental_summary_contains_only_aggregate_fields(self) -> None:
        summary = {
            "status": "PASS", "base_finish": "base-finish", "target_finish": "target-finish",
            "snapshot_change": "CHANGED", "diff_totals": {"added": 1, "removed": 2, "changed": 3,
                "technical_only": 4, "unchanged": 5}, "affected_case_count": 1, "hydrated_case_count": 1,
            "DIRECT": 1, "POTENTIAL": 0, "IRRELEVANT": 0, "prepared_record_count": 1,
            "batch_count": 1, "snapshot_client_dry_run": "PASS", "submission_eligible": True,
        }
        output = StringIO()
        with redirect_stdout(output):
            POC.write_snapshot_incremental_summary(summary)
        self.assertIn("Snapshot incremental dry-run: PASS", output.getvalue())
        self.assertIn("removed: 2", output.getvalue())
        self.assertNotIn("external_id", output.getvalue())
        self.assertNotIn("title", output.getvalue())

    def test_snapshot_incremental_base_artifact_validation_and_cleanup(self) -> None:
        stamp = "2026-10-20 02:00:40.000"
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            directory = root / "folketinget-base-manifest-39433055408"
            directory.mkdir()
            path = directory / "manifest.sqlite"
            create_v2_candidate_manifest(path, backup="a", finish=stamp,
                                        rows={entity: [] for entity in POC.REQUIRED_ENTITIES})
            context = POC.validate_snapshot_incremental_base_input(directory, "39433055408")
            self.assertEqual(context["run_id"], "39433055408")
            self.assertEqual(context["identity"], {
                "backup_sha256": "a" * 64,
                "backup_finish_date": stamp,
                "schema_fingerprint": "a" * 64,
            })
            with self.assertRaises(POC.PocError) as bad_id:
                POC.validate_snapshot_incremental_base_input(directory, "39433055408x")
            self.assertEqual(bad_id.exception.category, "workflow_run_id_invalid")
            (directory / "unexpected.txt").write_text("unexpected")
            with self.assertRaises(POC.PocError) as extra:
                POC.validate_snapshot_incremental_base_input(directory, "39433055408")
            self.assertEqual(extra.exception.category, "manifest_artifact_invalid")
            current = root / POC.INCREMENTAL_MANIFEST_DIRECTORY_NAME
            current.mkdir(mode=0o700)
            (current / "manifest.sqlite").write_bytes(b"temporary")
            POC.cleanup_incremental_manifest(current, root)
            self.assertFalse(current.exists())

    def test_snapshot_incremental_workflow_is_manual_and_never_has_live_credentials(self) -> None:
        workflow = (ROOT / ".github/workflows/folketinget-backup-poc.yml").read_text()
        script = (ROOT / "tools/folketinget_backup_poc.py").read_text()
        self.assertIn("on:\n  workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn("- SNAPSHOT_INCREMENTAL_DRY_RUN", workflow)
        self.assertIn("base_manifest_run_id:", workflow)
        self.assertIn('default: "39433055408"', workflow)
        self.assertIn("name: Validate snapshot incremental base manifest artifact", workflow)
        self.assertIn("name: Download snapshot incremental base manifest", workflow)
        self.assertIn("actions/download-artifact@v8", workflow)
        self.assertIn("INPUT_BASE_MANIFEST_RUN_ID:", workflow)
        self.assertIn("BASE_MANIFEST_PATH:", workflow)
        self.assertIn("name: Remove snapshot incremental temporary manifests", workflow)
        self.assertIn("inputs.mode == 'SNAPSHOT_INCREMENTAL_DRY_RUN'", workflow)
        self.assertIn("SNAPSHOT_INCREMENTAL_DRY_RUN =", script)
        self.assertIn("snapshot_ingestion_dry_run=True", script)
        incremental_body = script[script.index("def run_snapshot_incremental_dry_run"):script.index("def discover_candidate_provider_schema")]
        self.assertNotIn("submit_prepared_historical_records", incremental_body)
        self.assertIn("INGEST_API_TOKEN: ${{ inputs.mode == 'HISTORICAL_SNAPSHOT_BASELINE_LIVE' && secrets.INGEST_API_TOKEN || '' }}", workflow)

    def test_candidate_extractor_and_provider_failures_remain_safe(self) -> None:
        from folketinget_candidate_extraction import InMemoryFolketingetProvider

        stamp = "2026-10-19T02:00:00Z"
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            old, new = root / "old.sqlite", root / "new.sqlite"
            create_candidate_manifest(old, backup="1", finish="2026-10-18T02:00:00Z", include_sag=False)
            create_candidate_manifest(new, backup="2", finish=stamp, include_sag=False,
                                      extra_records={"Sag": [("PRIVATE KEY", b"f" * 32, b"s" * 32)]})
            with self.assertRaises(POC.PocError) as raised:
                POC.run_candidate_extraction(old, new, InMemoryFolketingetProvider({}), root, "old", stamp)
            self.assertEqual(raised.exception.category, "canonical_key_invalid")
            self.assertNotIn("PRIVATE KEY", str(raised.exception))

            key = json.dumps({"entity": "Sag", "key": [["id", "int", 42]]}, sort_keys=True, separators=(",", ":"))
            create_candidate_manifest(new, backup="2", finish=stamp, include_sag=True)

            class FailingProvider:
                def get_by_ids(self, entity, ids):
                    raise RuntimeError("SECRET PRIVATE TITLE row=42")
                def find_by_field(self, entity, field, values):
                    raise RuntimeError("SECRET PRIVATE TITLE row=42")

            result = POC.run_candidate_extraction(old, new, FailingProvider(), root, "old", stamp)
            self.assertFalse(result["complete"])
            output = json.dumps(result)
            for private in ("SECRET", "PRIVATE TITLE", "row=42", key):
                self.assertNotIn(private, output)
            self.assertIn({"code": "source_lookup_failed", "count": 1}, result["diagnostics"])

    def test_candidate_manifest_preflight_checks_identity_structure_and_chronology(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            old = root / "old"; old.mkdir()
            new = root / "new"; new.mkdir()
            create_candidate_manifest(old / "manifest.sqlite", backup="1", finish="2026-10-18T02:00:00Z", include_sag=False)
            create_candidate_manifest(new / "manifest.sqlite", backup="2", finish="2026-10-19T02:00:00Z", include_sag=True)
            context = POC.validate_candidate_manifest_inputs(old, new, "100", "101")
            self.assertEqual(context["report"]["totals"]["added"], 1)
            self.assertEqual(context["old_finish"], "2026-10-18T02:00:00Z")
            # Run IDs identify artifacts; manifest finish timestamps determine source chronology.
            self.assertEqual(
                POC.validate_candidate_manifest_inputs(old, new, "999", "100")["new_finish"],
                "2026-10-19T02:00:00Z",
            )
            with self.assertRaises(POC.PocError) as raised:
                POC.validate_candidate_manifest_inputs(old, new, "100", "100")
            self.assertEqual(raised.exception.category, "workflow_run_ids_must_differ")

    def test_candidate_backup_must_match_new_manifest_sha_finish_and_schema(self) -> None:
        context = {"report": {"new": {"backup_sha256": "b" * 64, "backup_finish_date": "finish"},
                              "schema_fingerprint": "c" * 64}}
        POC.validate_candidate_backup_identity("b" * 64, "finish", "c" * 64, context)
        for sha, finish, schema, category in (
            ("d" * 64, "finish", "c" * 64, "candidate_backup_sha_mismatch"),
            ("b" * 64, "other", "c" * 64, "candidate_backup_finish_mismatch"),
            ("b" * 64, "finish", "d" * 64, "candidate_schema_mismatch"),
        ):
            with self.subTest(category=category), self.assertRaises(POC.PocError) as raised:
                POC.validate_candidate_backup_identity(sha, finish, schema, context)
            self.assertEqual(raised.exception.category, category)

    def test_candidate_provider_uses_normalized_primary_key_metadata(self) -> None:
        from folketinget_sql_provider import PROJECTIONS, SQLServerFolketingetProvider

        entities = {"objects": {entity: ("dbo", "USER_TABLE") for entity in POC.REQUIRED_ENTITIES}}
        schemas = {
            entity: {
                "primary_key": ["id"],
                "columns": [{"name": field, "type_name": "int"} for field in PROJECTIONS[entity]],
            }
            for entity in POC.REQUIRED_ENTITIES
        }
        provider = POC.create_candidate_sql_provider("private", entities, schemas)
        self.assertIsInstance(provider, SQLServerFolketingetProvider)
        with self.assertRaises(POC.PocError) as raised:
            POC.create_candidate_sql_provider(
                "private", entities,
                {entity: {"schema_name": "dbo", "columns": schemas[entity]["columns"]}
                 for entity in POC.REQUIRED_ENTITIES},
            )
        self.assertEqual(raised.exception.category, "candidate_provider_schema_invalid")

    def test_candidate_bundle_is_private_temporary_and_removed_after_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            old, new = root / "old.sqlite", root / "new.sqlite"
            create_candidate_manifest(old, backup="1", finish="2026-10-18T02:00:00Z", include_sag=False)
            create_candidate_manifest(new, backup="2", finish="2026-10-19T02:00:00Z", include_sag=True)
            provider = SimpleNamespace()
            # The extraction result is synthetic and deliberately contains private content;
            # only its aggregate summary may leave this helper.
            fake_bundle = {
                "format_version": 1,
                "source": "folketinget",
                "candidate_case_count": 1,
                "cases": [{"titel": "PRIVATE TITLE", "external_id": "private-id"}],
                "complete": True,
                "diagnostics": [],
                "diff": {
                    "entities": {entity: dict.fromkeys(POC.snapshot_diff.CATEGORIES, 0)
                                 for entity in POC.REQUIRED_ENTITIES},
                    "totals": dict.fromkeys(POC.snapshot_diff.CATEGORIES, 0),
                },
                "_summary": {"affected_case_count": 1, "hydrated_case_count": 1},
            }
            fake_bundle["diff"]["totals"]["added"] = 1
            temp_files_before = set(root.iterdir())
            with mock.patch("folketinget_candidate_extraction.extract_candidates", return_value=fake_bundle):
                summary = POC.run_candidate_extraction(
                    old, new, provider, root, "old-finish", "new-finish"
                )
            self.assertEqual(set(root.iterdir()), temp_files_before)
            rendered = json.dumps(summary)
            self.assertNotIn("PRIVATE TITLE", rendered)
            self.assertNotIn("private-id", rendered)
            self.assertEqual(summary["candidate_case_count"], 1)

    def test_candidate_workflow_is_manual_and_does_not_publish_candidate_bundle(self) -> None:
        workflow = (ROOT / ".github/workflows/folketinget-backup-poc.yml").read_text()
        self.assertIn("- EXTRACT_SNAPSHOT_CANDIDATES", workflow)
        self.assertIn("- CLASSIFY_SNAPSHOT_CANDIDATES", workflow)
        self.assertIn("- PREPARE_SNAPSHOT_INGESTION", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn("inputs.mode == 'DIFF_MANIFESTS' || inputs.mode == 'EXTRACT_SNAPSHOT_CANDIDATES' || inputs.mode == 'CLASSIFY_SNAPSHOT_CANDIDATES' || inputs.mode == 'PREPARE_SNAPSHOT_INGESTION' || inputs.mode == 'SNAPSHOT_INGESTION_DRY_RUN'", workflow)
        self.assertIn("OLD_MANIFEST_PATH:", workflow)
        self.assertIn("NEW_MANIFEST_PATH:", workflow)
        self.assertIn("INPUT_OLD_RUN_ID:", workflow)
        self.assertIn("INPUT_NEW_RUN_ID:", workflow)
        self.assertIn("if: inputs.mode != 'DIFF_MANIFESTS' && inputs.mode != 'HISTORICAL_SNAPSHOT_PRODUCTION_PREFLIGHT'", workflow)
        self.assertLess(workflow.index("name: Verify and restore official backup"),
                        workflow.index("name: Remove downloaded manifest artifacts"))
        self.assertNotIn("candidate-bundle", workflow)
        self.assertNotIn("EXTRACT_SNAPSHOT_CANDIDATES\n        uses: actions/upload-artifact", workflow)
        self.assertIn("if: success() && inputs.mode == 'CREATE_MANIFEST_BASELINE'", workflow)

    def test_classify_mode_is_explicit_and_restores_without_production_integrations(self) -> None:
        self.assertEqual(POC.validate_mode("CLASSIFY_SNAPSHOT_CANDIDATES"), "CLASSIFY_SNAPSHOT_CANDIDATES")
        self.assertEqual(POC.validate_mode("PREPARE_SNAPSHOT_INGESTION"), "PREPARE_SNAPSHOT_INGESTION")
        workflow = (ROOT / ".github/workflows/folketinget-backup-poc.yml").read_text()
        self.assertIn("on:\n  workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn("/internal/snapshot-readiness?source=folketinget", workflow)
        self.assertNotIn("/internal/ingest", workflow)
        self.assertNotIn("/internal/notifications/", workflow)
        self.assertNotIn("sendMessage", workflow)
        self.assertNotIn("notification", workflow.lower())

    def test_repository_imports_work_from_repository_and_external_cwd(self) -> None:
        script_path = ROOT / "tools" / "folketinget_backup_poc.py"
        code = f"""
import importlib.util
import sys
script = {str(script_path)!r}
spec = importlib.util.spec_from_file_location('isolated_backup_poc', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.REPOSITORY_ROOT == {str(ROOT)!r}
assert sys.path.count(module.REPOSITORY_ROOT) == 1
from collectors.folketinget_models import normalize_many
assert callable(normalize_many)
try:
    module.exercise_sql_provider(None, {{}})
except module.PocError as error:
    assert error.category == 'sql_provider_sample_validation_failed'
else:
    raise AssertionError('invalid smoke fixture unexpectedly passed')
"""
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as external_directory:
            for working_directory in (ROOT, pathlib.Path(external_directory)):
                with self.subTest(working_directory=str(working_directory)):
                    result = subprocess.run(
                        [sys.executable, "-c", code],
                        cwd=working_directory,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, msg="isolated import probe failed")

    def test_confirmation_must_be_exact_true(self) -> None:
        for value in ("", "false", "TRUE", "yes"):
            with self.subTest(value=value), self.assertRaises(POC.PocError):
                POC.validate_confirmation(value)
        POC.validate_confirmation("true")

    def test_both_credentials_are_required(self) -> None:
        for username, password in (("", "secret"), ("user", ""), ("", "")):
            with self.subTest(username=bool(username), password=bool(password)), self.assertRaises(POC.PocError):
                POC.validate_credentials(username, password)

    def test_only_x64_ubuntu_2404_is_accepted(self) -> None:
        POC.require_x64_ubuntu24("x86_64", "24.04")
        for machine, release in (("aarch64", "24.04"), ("x86_64", "22.04")):
            with self.subTest(machine=machine, release=release), self.assertRaises(POC.PocError):
                POC.require_x64_ubuntu24(machine, release)

    def test_resource_guards_fail_closed(self) -> None:
        with self.assertRaises(POC.PocError):
            POC.check_free_space(10, 11)
        with self.assertRaises(POC.PocError):
            POC.download_space_required(0)
        with self.assertRaises(POC.PocError):
            POC.download_space_required(POC.MAX_BACKUP_BYTES + 1)
        with self.assertRaises(POC.PocError):
            POC.restore_space_required([])

    def test_restore_disk_space_parser_accepts_numeric_available_bytes(self) -> None:
        self.assertEqual(POC.parse_df_available_bytes("Avail\n123456789\n"), 123456789)

    def test_restore_disk_space_command_failure_is_categorized(self) -> None:
        with mock.patch.object(
            POC,
            "run_checked",
            side_effect=POC.PocError("restore_disk_space_check_failed"),
        ):
            with self.assertRaises(POC.PocError) as raised:
                POC.check_restore_disk_space(pathlib.Path("/restricted/path"), 1)
        self.assertEqual(str(raised.exception), "restore_disk_space_check_failed")

    def test_restore_disk_space_parser_rejects_malformed_results(self) -> None:
        for output in ("", "Avail\n\n", "Avail\n-1\n", "Avail\n1.5\n", "Avail\n1\nextra\n", "Wrong\n1\n"):
            with self.subTest(output=output), self.assertRaises(POC.PocError) as raised:
                POC.parse_df_available_bytes(output)
            self.assertEqual(str(raised.exception), "restore_disk_space_result_invalid")

    def test_backup_path_permissions_and_size_are_checked_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backup = pathlib.Path(directory) / "oda.bak"
            with mock.patch.object(POC, "run_checked", side_effect=POC.PocError("backup_file_validation_failed")):
                with self.assertRaises(POC.PocError) as raised:
                    POC.validate_backup_readable_by_sql_server(backup, 10)
            self.assertEqual(str(raised.exception), "backup_file_validation_failed")
            with mock.patch.object(POC, "run_checked", side_effect=["regular file 640 6\n", None]) as checked:
                POC.validate_backup_readable_by_sql_server(backup, 6)
            self.assertIn("stat", checked.call_args_list[0].args[0])
            self.assertEqual(checked.call_args_list[1].args[1], "backup_path_unreadable_by_sql_server")
            with mock.patch.object(POC, "run_checked", side_effect=["regular file 640 6\n", POC.PocError("backup_path_unreadable_by_sql_server")]):
                with self.assertRaises(POC.PocError) as raised:
                    POC.validate_backup_readable_by_sql_server(backup, 6)
            self.assertEqual(str(raised.exception), "backup_path_unreadable_by_sql_server")

    def test_backup_file_permissions_reject_world_access_and_wrong_size(self) -> None:
        for stat_output in ("regular file 644 6\n", "regular file 640 7\n", "directory 750 6\n", "invalid\n"):
            with self.subTest(stat_output=stat_output):
                with mock.patch.object(POC, "run_checked", return_value=stat_output):
                    with self.assertRaises(POC.PocError) as raised:
                        POC.validate_backup_readable_by_sql_server(pathlib.Path("/var/opt/mssql/backup/poc/oda.bak"), 6)
                self.assertEqual(str(raised.exception), "backup_file_validation_failed")

    def test_download_metadata_and_header_parsing_have_safe_categories(self) -> None:
        with self.assertRaises(POC.PocError) as raised:
            POC.validate_download_metadata({"file_size_bytes": "bad"}, 10)
        self.assertEqual(str(raised.exception), "backup_download_metadata_invalid")
        malformed_header = "SoftwareVersionMajor|BackupType|IsDamaged|Compressed|BackupFinishDate\n---|---|---|---|---\nbad|1|0|1|date\n"
        with self.assertRaises(POC.PocError) as raised:
            POC.header_metadata(malformed_header)
        self.assertEqual(str(raised.exception), "backup_header_invalid")
        malformed_filelist = "LogicalName|Type|Size\n---|---|---\nODA|D|not-an-integer\n"
        with self.assertRaises(POC.PocError) as raised:
            POC.filelist_metadata(malformed_filelist)
        self.assertEqual(str(raised.exception), "backup_filelist_invalid")

    def test_headeronly_uses_official_result_column_names(self) -> None:
        fixture = (
            "BackupName|BackupType|BackupFinishDate|SoftwareVersionMajor|IsDamaged|Compressed\n"
            "----------|----------|----------------|--------------------|---------|----------\n"
            "ODA nightly|1|2026-10-17 02:14:06.000|17|0|1\n"
            "(1 rows affected)\n"
        )
        parsed = POC.header_metadata(fixture)
        self.assertEqual(parsed["source_sql_major"], 17)
        self.assertEqual(parsed["backup_type"], 1)
        self.assertEqual(parsed["compressed"], "1")
        self.assertEqual(parsed["backup_finish_date"], "2026-10-17 02:14:06.000")

        undocumented_alias_fixture = fixture.replace("|Compressed", "|IsCompressed")
        with self.assertRaises(POC.PocError) as raised:
            POC.header_metadata(undocumented_alias_fixture)
        self.assertEqual(str(raised.exception), "sql_metadata_field_missing")

    def test_cleanup_still_removes_temp_path_if_service_check_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = POC.BACKUP_ROOT / "denmarkbot-poc-backup-test"
            calls: list[list[str]] = []

            def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
                calls.append(argv)
                if "systemctl" in argv:
                    raise OSError("private service error")
                return SimpleNamespace(returncode=0)

            with mock.patch.dict(os.environ, {"RUNNER_TEMP": directory}):
                with mock.patch.object(POC.subprocess, "run", side_effect=fake_run):
                    with self.assertRaises(POC.PocError) as raised:
                        POC.cleanup("never-printed", backup_dir, None)
            self.assertEqual(str(raised.exception), "temporary_data_cleanup_failed")
            self.assertTrue(any("rm" in call for call in calls))

    def test_backup_path_allowlist_and_cleanup_use_sql_server_staging_root(self) -> None:
        runner_temp = pathlib.Path("/runner/temp")
        backup_dir = POC.BACKUP_ROOT / "denmarkbot-poc-backup-run-id-random"
        self.assertTrue(POC.cleanup_path_is_allowed(backup_dir, runner_temp))
        self.assertTrue(POC.cleanup_path_is_allowed(runner_temp / "denmarkbot-poc-download-run-id-random", runner_temp))
        self.assertFalse(POC.cleanup_path_is_allowed(pathlib.Path("/tmp/denmarkbot-poc-backup-outside"), runner_temp))
        self.assertFalse(POC.cleanup_path_is_allowed(pathlib.Path("/var/opt/mssql/backup/../outside"), runner_temp))

        calls: list[list[str]] = []
        with mock.patch.object(POC.subprocess, "run", side_effect=lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0)):
            POC.cleanup(None, backup_dir, None)
        self.assertIn(str(backup_dir), calls[0])

    def test_backup_staging_permissions_are_private_and_sql_server_readable(self) -> None:
        script = (ROOT / "tools/folketinget_backup_poc.py").read_text()
        self.assertIn('"-g", "mssql", "-m", "0750", "--", str(backup_dir)', script)
        self.assertIn('"-g", "mssql", "-m", "0640", "--", str(download_temp_path), str(backup_path)', script)
        self.assertIn('["sudo", "-u", "mssql", "test", "-r", str(path)]', script)

    def test_cleanup_rejects_path_outside_allowlisted_directories(self) -> None:
        calls: list[list[str]] = []
        with mock.patch.object(POC.subprocess, "run", side_effect=lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0)):
            with self.assertRaises(POC.PocError) as raised:
                POC.cleanup(None, pathlib.Path("/tmp/not-an-allowed-poc-path"), None)
        self.assertEqual(str(raised.exception), "temporary_data_cleanup_failed")
        self.assertEqual(calls, [])

    def test_backup_must_be_full_and_supported(self) -> None:
        POC.validate_backup_header(16, 1, False)
        for args in ((18, 1, False), (16, 2, False), (16, 1, True)):
            with self.subTest(args=args), self.assertRaises(POC.PocError):
                POC.validate_backup_header(*args)

    def test_redirect_origin_rejects_credentials_and_other_hosts(self) -> None:
        self.assertEqual(POC.official_origin("https://oda.ft.dk/file"), POC.ODA_ORIGIN)
        self.assertIsNone(POC.official_origin("https://user:pass@oda.ft.dk/file"))
        self.assertNotEqual(POC.official_origin("https://example.org/file"), POC.ODA_ORIGIN)

    def test_repository_config_must_be_official_and_expected(self) -> None:
        valid = b"deb [arch=amd64] https://packages.microsoft.com/ubuntu/24.04/mssql-server-2025 noble main\n"
        self.assertEqual(POC.validate_repository_list(valid, "mssql-server-2025"), valid)
        with self.assertRaises(POC.PocError):
            POC.validate_repository_list(
                b"deb https://evil.example/ubuntu/24.04/mssql-server-2025 noble main\n",
                "mssql-server-2025",
            )

    def test_microsoft_key_download_failure_is_sanitized(self) -> None:
        with mock.patch.object(POC.urllib.request, "urlopen", side_effect=urllib.error.URLError("private response detail")):
            with self.assertRaises(POC.PocError) as raised:
                POC.fetch_microsoft_file(
                    "https://packages.microsoft.com/keys/microsoft.asc",
                    "microsoft_key_download_failed",
                )
        self.assertEqual(str(raised.exception), "microsoft_key_download_failed")

    def test_apt_update_failure_has_its_own_safe_category(self) -> None:
        key = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nfixture\n"
        server_repo = b"deb [arch=amd64] https://packages.microsoft.com/ubuntu/24.04/mssql-server-2025 noble main\n"
        tools_repo = b"deb [arch=amd64] https://packages.microsoft.com/ubuntu/24.04/prod noble main\n"

        def fail_on_apt_update(argv: list[str], category: str, **kwargs: object) -> str:
            if "update" in argv:
                raise POC.PocError(category)
            return ""

        with mock.patch.object(POC, "fetch_microsoft_file", side_effect=[key, server_repo, tools_repo]):
            with mock.patch.object(POC, "run_checked", side_effect=fail_on_apt_update):
                with self.assertRaises(POC.PocError) as raised:
                    POC.run_sql_server_setup("never-printed")
        self.assertEqual(str(raised.exception), "apt_update_failed")

    def test_package_install_failure_has_its_own_safe_category(self) -> None:
        key = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nfixture\n"
        server_repo = b"deb [arch=amd64] https://packages.microsoft.com/ubuntu/24.04/mssql-server-2025 noble main\n"
        tools_repo = b"deb [arch=amd64] https://packages.microsoft.com/ubuntu/24.04/prod noble main\n"

        def fail_on_package_install(argv: list[str], category: str, **kwargs: object) -> str:
            if "mssql-server" in argv and "install" in argv:
                raise POC.PocError(category)
            return ""

        with mock.patch.object(POC, "fetch_microsoft_file", side_effect=[key, server_repo, tools_repo]):
            with mock.patch.object(POC, "run_checked", side_effect=fail_on_package_install):
                with self.assertRaises(POC.PocError) as raised:
                    POC.run_sql_server_setup("never-printed")
        self.assertEqual(str(raised.exception), "sql_server_package_install_failed")

    def test_restore_sql_escapes_metadata(self) -> None:
        query = POC.build_restore_query(
            "/tmp/oda.bak",
            "/tmp/restore",
            [{"logical_name": "data'name", "type": "D", "size": 1024}],
        )
        self.assertIn("[DenmarkBotFolketingetBackupPoc]", query)
        self.assertIn("data''name", query)
        self.assertIn("/tmp/restore/file-1.mdf", query)

    def test_sqlcmd_parser_excludes_headers_and_separators(self) -> None:
        output = "name|type_desc|size\n----|---------|----\nSag|USER_TABLE|8192\n(1 rows affected)\n"
        headers, rows = POC.parse_sqlcmd_table(output)
        self.assertEqual(headers, ["name", "type_desc", "size"])
        self.assertEqual(rows, [["Sag", "USER_TABLE", "8192"]])

    def test_post_dbcc_metadata_parsers_fail_with_safe_categories(self) -> None:
        for output in ("", "0\n", "-1\n", "not-a-size\n"):
            with self.subTest(parser="database_size", output=output), self.assertRaises(POC.PocError) as raised:
                POC.parse_restored_database_size(output)
            self.assertEqual(str(raised.exception), "restored_database_size_invalid")
        self.assertEqual(POC.parse_restored_database_size("21474836480\n"), 21_474_836_480)

        for output in ("", "-1\n", "many\n"):
            with self.subTest(parser="entity_count", output=output), self.assertRaises(POC.PocError) as raised:
                POC.parse_entity_count(output)
            self.assertEqual(str(raised.exception), "entity_count_result_invalid")
        self.assertEqual(POC.parse_entity_count("0\n"), 0)

        self.assertEqual(
            POC.parse_entity_discovery(
                "schema_name|entity_name|object_type\n-----------|-----------|-----------\ndbo|Sag|USER_TABLE\n"
            ),
            {"Sag": ("dbo", "USER_TABLE")},
        )
        self.assertEqual(
            POC.parse_primary_key_metadata("key_ordinal|column_name\n-----------|------------\n1|SagId\n"),
            ["SagId"],
        )
        self.assertEqual(
            POC.parse_foreign_key_metadata(
                "parent_table|parent_column|referenced_table|referenced_column\n"
                "------------|-------------|----------------|------------------\nSag|CaseId|Case|Id\n"
            ),
            [("Sag", "CaseId", "Case", "Id")],
        )

        for parser, output, category in (
            (POC.parse_entity_discovery, "bad output", "entity_discovery_result_invalid"),
            (POC.parse_primary_key_metadata, "bad output", "primary_key_metadata_invalid"),
            (POC.parse_foreign_key_metadata, "bad output", "foreign_key_metadata_invalid"),
        ):
            with self.subTest(parser=parser.__name__), self.assertRaises(POC.PocError) as raised:
                parser(output)
            self.assertEqual(str(raised.exception), category)

    def test_post_dbcc_sql_failures_have_specific_safe_categories(self) -> None:
        cases = (
            ("database_size", POC.restored_database_size, "restored_database_size_query_failed"),
            ("discovery", POC.table_objects, "entity_discovery_query_failed"),
        )
        for name, operation, category in cases:
            with self.subTest(query=name), mock.patch.object(POC, "sqlcmd", side_effect=POC.PocError(category)) as sql:
                with self.assertRaises(POC.PocError) as raised:
                    operation("private-password")
            self.assertEqual(str(raised.exception), category)
            self.assertEqual(sql.call_args.kwargs["failure_category"], category)
        with mock.patch.object(POC, "sqlcmd", return_value="21474836480\n") as sql:
            self.assertEqual(POC.restored_database_size("private-password"), 21_474_836_480)
        self.assertIn("SUM(CAST(size AS bigint))", sql.call_args.args[0])

        with mock.patch.object(POC, "table_objects", return_value={"Sag": ("dbo", "USER_TABLE")}):
            scenarios = (
                ("entity_count_query_failed", [POC.PocError("entity_count_query_failed")]),
                ("primary_key_metadata_query_failed", ["1\n", POC.PocError("primary_key_metadata_query_failed")]),
                (
                    "foreign_key_metadata_query_failed",
                    ["1\n", "key_ordinal|column_name\n-----------|------------\n1|SagId\n", POC.PocError("foreign_key_metadata_query_failed")],
                ),
            )
            for target_category, responses in scenarios:
                with self.subTest(query=target_category):
                    with mock.patch.object(POC, "sqlcmd", side_effect=responses):
                        with redirect_stdout(StringIO()), self.assertRaises(POC.PocError) as raised:
                            POC.validate_entities("private-password")
                    self.assertEqual(str(raised.exception), target_category)

    def test_post_dbcc_progress_markers_are_ordered(self) -> None:
        source = (ROOT / "tools/folketinget_backup_poc.py").read_text()
        main_body = source[source.index("def main()") :]
        markers = ("dbcc_pass", "database_size_check", "summary_write")
        positions = [main_body.index(f"progress={marker}") for marker in markers]
        self.assertEqual(positions, sorted(positions))
        entity_body = source[source.index("def validate_entities(") : source.index("def write_summary(")]
        entity_markers = ("entity_discovery", "entity_counts", "primary_key_metadata", "foreign_key_metadata")
        positions = [entity_body.index(f"progress={marker}") for marker in entity_markers]
        self.assertEqual(positions, sorted(positions))

    def test_post_dbcc_summary_omits_schema_contents(self) -> None:
        summary = {
            "download": {"sha256": "a" * 64, "download_seconds": 1},
            "backup_size_bytes": 10,
            "header": {"source_sql_major": 17, "backup_finish_date": "date", "compressed": "1"},
            "restored_database_size_bytes": 20,
            "entities": {
                "objects": {"Sag": ("PrivateSchema", "USER_TABLE")},
                "counts": {"Sag": 3},
                "primary_keys": {"Sag": ["PrivateColumn"]},
                "foreign_keys": [("Sag", "PrivateColumn", "Other", "OtherId")],
            },
        }
        output = StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}), redirect_stdout(output):
            POC.write_summary(summary)
        rendered = output.getvalue()
        for private_schema_value in ("PrivateSchema", "PrivateColumn", "OtherId"):
            self.assertNotIn(private_schema_value, rendered)
        self.assertIn("Required entities present: 1/6", rendered)
        self.assertIn("Rows across required entities: 3", rendered)

    def test_filelist_accepts_only_data_and_log_files(self) -> None:
        headers = "LogicalName|Type|Size"
        output = f"{headers}\n-----------|----|----\nODA_Data|D|1024\nODA_Log|L|512\n"
        entries = POC.filelist_metadata(output)
        self.assertEqual([entry["type"] for entry in entries], ["D", "L"])
        invalid = f"{headers}\n-----------|----|----\nODA_Data|F|1024\n"
        with self.assertRaises(POC.PocError):
            POC.filelist_metadata(invalid)

    def test_workflow_is_manual_only_and_uses_required_safeguards(self) -> None:
        workflow = (ROOT / ".github/workflows/folketinget-backup-poc.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertNotIn("runs-on: ubuntu-latest", workflow)
        self.assertIn("uses: actions/checkout@v5", workflow)
        self.assertIn("confirm_restore", workflow)
        self.assertIn("secrets.ODA_BACKUP_USERNAME", workflow)
        self.assertIn("secrets.ODA_BACKUP_PASSWORD", workflow)
        self.assertIn("uses: actions/upload-artifact@v7", workflow)
        baseline_upload = workflow.split("- name: Upload validated manifest only", 1)[1].split("- name: Remove temporary manifest files", 1)[0]
        self.assertIn("retention-days: 30", baseline_upload)
        self.assertIn("CREATE_MANIFEST_BASELINE", workflow)
        self.assertIn("VALIDATE_SQL_PROVIDER", workflow)
        self.assertIn("if: inputs.mode != 'DIFF_MANIFESTS' && inputs.mode != 'HISTORICAL_SNAPSHOT_PRODUCTION_PREFLIGHT'", workflow)
        self.assertIn("manifest.sqlite", workflow)
        self.assertNotIn(".bak", workflow)
        self.assertNotIn("telegram", workflow.lower())
        self.assertIn("/internal/snapshot-readiness?source=folketinget", workflow)
        self.assertNotIn("/internal/ingest", workflow)

    def test_restore_script_has_no_production_or_telegram_route(self) -> None:
        script = (ROOT / "tools/folketinget_backup_poc.py").read_text().lower()
        for forbidden in ("workers.dev", "/internal/", "api.telegram.org", "ingestion endpoint"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, script)

    def test_sql_server_setup_is_verified_before_backup_download(self) -> None:
        script = (ROOT / "tools/folketinget_backup_poc.py").read_text()
        main_body = script[script.index("def main()") :]
        self.assertLess(main_body.index("run_sql_server_setup(password)"), main_body.index("request_content_length(opener)"))
        self.assertLess(main_body.index("request_content_length(opener)"), main_body.index("download_backup(opener"))

    def test_safe_restore_progress_markers_are_present_in_order(self) -> None:
        script = (ROOT / "tools/folketinget_backup_poc.py").read_text()
        main_body = script[script.index("def main()") :]
        markers = (
            "backup_download_complete",
            "restore_storage_check",
            "restore_header_check",
            "restore_filelist_check",
            "restore_start",
            "dbcc_start",
        )
        positions = [main_body.index(f"progress={marker}") for marker in markers]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
