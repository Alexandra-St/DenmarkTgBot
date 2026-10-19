from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from collectors.relevance import DIRECT, IRRELEVANT, POTENTIAL

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "folketinget_historical_baseline", ROOT / "tools" / "folketinget_historical_baseline.py"
)
BASELINE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(BASELINE)

from folketinget_candidate_extraction import InMemoryFolketingetProvider

ENTITIES = ("Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus")
STAMP = "2026-10-18T12:00:00Z"


def canonical_key(entity: str, identifier: int) -> str:
    return json.dumps({"entity": entity, "key": [["id", "int", identifier]]}, sort_keys=True, separators=(",", ":"))


def create_manifest(path: pathlib.Path, sag_ids: list[int], *, declared_sag_count: int | None = None) -> None:
    total = len(sag_ids)
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE snapshot_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), backup_sha256 TEXT NOT NULL, "
            "backup_finish_date TEXT NOT NULL, source_sql_major INTEGER NOT NULL, schema_fingerprint TEXT NOT NULL, "
            "generated_at TEXT NOT NULL, total_rows INTEGER NOT NULL);"
            "CREATE TABLE entity_counts (entity TEXT PRIMARY KEY, row_count INTEGER NOT NULL) WITHOUT ROWID;"
            'CREATE TABLE "rows" (entity TEXT NOT NULL, canonical_key TEXT NOT NULL, full_hash BLOB NOT NULL CHECK(length(full_hash)=32), '
            "semantic_hash BLOB NOT NULL CHECK(length(semantic_hash)=32), PRIMARY KEY(entity, canonical_key)) WITHOUT ROWID;"
        )
        db.execute("INSERT INTO snapshot_metadata VALUES (1, ?, ?, 17, ?, ?, ?)",
                   ("a" * 64, "2026-10-18T02:00:00Z", "b" * 64, "2026-10-18T03:00:00Z", total))
        for entity in ENTITIES:
            db.execute("INSERT INTO entity_counts VALUES (?, ?)", (entity, declared_sag_count if entity == "Sag" and declared_sag_count is not None else len(sag_ids) if entity == "Sag" else 0))
        db.executemany('INSERT INTO "rows" VALUES (?, ?, ?, ?)',
                       (("Sag", canonical_key("Sag", identifier), b"f" * 32, b"s" * 32) for identifier in sag_ids))


def source_rows(sag_ids: list[int]) -> dict[str, list[dict[str, object]]]:
    titles = ["Fordrevne fra Ukraine", "Ukraine og opholdstilladelse", "Tekniske regler"]
    return {"Sag": [
        {"id": identifier, "nummer": str(identifier), "titel": titles[index % len(titles)], "typeid": None,
         "kategoriid": None, "statusid": None, "opdateringsdato": STAMP, "retsinformationsurl": None}
        for index, identifier in enumerate(sag_ids)
    ]}


class TrackingProvider(InMemoryFolketingetProvider):
    def __init__(self, rows):
        super().__init__(rows)
        self.lookup_sizes: list[int] = []

    def get_by_ids(self, entity, ids):
        self.lookup_sizes.append(len(ids))
        return super().get_by_ids(entity, ids)

    def find_by_field(self, entity, field, values):
        self.lookup_sizes.append(len(values))
        return super().find_by_field(entity, field, values)


class FolketingetHistoricalBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.manifest = self.root / "manifest.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def run_fixture(self, ids: list[int], *, page_size: int = 250):
        create_manifest(self.manifest, ids)
        provider = TrackingProvider(source_rows(ids))
        result = BASELINE.run_historical_baseline(self.manifest, provider, batch_size=page_size)
        return result, provider

    def test_deterministic_bounded_enumeration_and_multi_batch_hydration(self):
        create_manifest(self.manifest, [8, 2, 5, 1, 9])
        batches = list(BASELINE._iter_sag_id_batches(self.manifest, 2))
        self.assertEqual(batches, [(1, 2), (5, 8), (9,)])
        provider = TrackingProvider(source_rows([8, 2, 5, 1, 9]))
        result = BASELINE.run_historical_baseline(self.manifest, provider, batch_size=2)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["manifest_sag_count"], 5)
        self.assertEqual(result["hydrated_case_count"], 5)
        self.assertEqual(result["prepared_record_count"], 5)
        self.assertEqual(max(provider.lookup_sizes), 2)
        self.assertLessEqual(result["largest_batch_records"], 50)

    def test_historical_records_and_relevance_aggregates(self):
        result, _ = self.run_fixture([1, 2, 3])
        self.assertEqual(result["DIRECT"], 1)
        self.assertEqual(result["POTENTIAL"], 1)
        self.assertEqual(result["IRRELEVANT"], 1)
        original = BASELINE.ingestion_client.prepare_records
        prepared = []
        def capture(*args, **kwargs):
            records = original(*args, **kwargs)
            prepared.extend(records)
            return records
        with mock.patch.object(BASELINE.ingestion_client, "prepare_records", wraps=BASELINE.ingestion_client.prepare_records) as prepare:
            prepare.side_effect = capture
            BASELINE.run_historical_baseline(self.manifest, TrackingProvider(source_rows([1, 2, 3])))
            self.assertTrue(all(call.kwargs["mode"] == "historical" for call in prepare.call_args_list))
        self.assertTrue(all(record["historical"] is True for record in prepared))

    def test_existing_relation_hydration_assembles_full_case_graph(self):
        create_manifest(self.manifest, [7])
        rows = source_rows([7])
        rows["Sag"][0]["statusid"] = 70
        rows.update({
            "Sagstrin": [{"id": 71, "sagid": 7, "titel": "Stage", "dato": None, "typeid": None,
                          "statusid": None, "opdateringsdato": STAMP}],
            "SagDokument": [{"id": 72, "sagid": 7, "dokumentid": 73, "bilagsnummer": None,
                             "frigivelsesdato": None, "rolleid": None, "opdateringsdato": STAMP}],
            "Dokument": [{"id": 73, "titel": "Document", "dato": None, "modtagelsesdato": None,
                          "frigivelsesdato": None, "opdateringsdato": STAMP}],
            "Afstemning": [{"id": 74, "sagstrinid": 71, "nummer": 1, "vedtaget": True,
                            "konklusion": None, "kommentar": None, "mødeid": None, "typeid": None,
                            "opdateringsdato": STAMP}],
            "Sagsstatus": [{"id": 70, "status": "Active", "opdateringsdato": STAMP}],
        })
        provider = TrackingProvider(rows)
        result = BASELINE.run_historical_baseline(self.manifest, provider)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["hydrated_case_count"], 1)
        self.assertTrue({1}.issubset(set(provider.lookup_sizes)))

    def test_manifest_count_mismatch_fails_closed(self):
        create_manifest(self.manifest, [1], declared_sag_count=2)
        with self.assertRaises(BASELINE.HistoricalBaselineError) as raised:
            BASELINE.run_historical_baseline(self.manifest, TrackingProvider(source_rows([1])))
        self.assertEqual(raised.exception.category, "manifest_row_count_mismatch")

    def test_missing_sag_and_missing_required_relation_are_incomplete(self):
        create_manifest(self.manifest, [1, 2])
        provider = TrackingProvider(source_rows([1]))
        result = BASELINE.run_historical_baseline(self.manifest, provider)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertGreater(result["incomplete_case_count"], 0)
        self.assertGreater(result["diagnostics"].get("hydrated_sag_mismatch", 0), 0)

    def test_malformed_canonical_sag_key_is_rejected_without_identifier(self):
        create_manifest(self.manifest, [1])
        with sqlite3.connect(self.manifest) as db:
            db.execute('INSERT INTO "rows" VALUES (?, ?, ?, ?)', ("Sag", '{"entity":"Sag","key":[["id","int",true]]}', b"x" * 32, b"y" * 32))
            db.execute("UPDATE entity_counts SET row_count=2 WHERE entity='Sag'")
            db.execute("UPDATE snapshot_metadata SET total_rows=2")
        with self.assertRaises(BASELINE.HistoricalBaselineError) as raised:
            BASELINE.run_historical_baseline(self.manifest, TrackingProvider(source_rows([1])))
        self.assertEqual(raised.exception.category, "canonical_key_invalid")

    def test_duplicate_decoded_sag_id_is_rejected(self):
        create_manifest(self.manifest, [1, 2])
        with mock.patch.object(BASELINE.candidate_extraction, "decode_canonical_key", return_value=1):
            with self.assertRaises(BASELINE.HistoricalBaselineError) as raised:
                list(BASELINE._iter_sag_id_batches(self.manifest, 10))
        self.assertEqual(raised.exception.category, "duplicate_sag_key")

    def test_no_network_or_checkpoint_helpers_are_called(self):
        from collectors import snapshot_ingestion_client
        create_manifest(self.manifest, [1])
        with mock.patch.object(BASELINE.ingestion_client, "upload", side_effect=AssertionError("upload")), \
                mock.patch.object(BASELINE.ingestion_client, "proposed_checkpoints", side_effect=AssertionError("checkpoints")), \
                mock.patch.object(BASELINE.ingestion_client, "collection_complete", side_effect=AssertionError("complete")), \
                mock.patch.object(BASELINE.ingestion_client, "_request", side_effect=AssertionError("network")), \
                mock.patch.object(snapshot_ingestion_client, "_post", side_effect=AssertionError("snapshot HTTP")):
            result = BASELINE.run_historical_baseline(self.manifest, TrackingProvider(source_rows([1])))
        self.assertEqual(result["status"], "PASS")

    def test_summary_does_not_leak_identifiers_titles_or_payload(self):
        result, _ = self.run_fixture([918273])
        text = json.dumps(result, ensure_ascii=False)
        for secret in ("918273", "Fordrevne fra Ukraine", "source_url", "normalized_snapshot"):
            self.assertNotIn(secret, text)

    def test_zero_sag_manifest_is_complete(self):
        result, _ = self.run_fixture([])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["manifest_sag_count"], 0)
        self.assertEqual(result["prepared_record_count"], 0)

    def test_progress_callback_receives_only_aggregate_counts(self):
        create_manifest(self.manifest, [1, 2])
        progress = []
        BASELINE.run_historical_baseline(
            self.manifest, TrackingProvider(source_rows([1, 2])), batch_size=1,
            progress_callback=lambda processed, total: progress.append((processed, total)),
        )
        self.assertEqual(progress, [(1, 2), (2, 2)])


if __name__ == "__main__":
    unittest.main()
