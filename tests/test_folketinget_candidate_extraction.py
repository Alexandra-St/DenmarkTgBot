from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import tempfile
import unittest
from collections import Counter

from collectors.relevance import DIRECT, classify


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "folketinget_candidate_extraction", ROOT / "tools" / "folketinget_candidate_extraction.py"
)
CANDIDATES = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CANDIDATES)

ENTITIES = ("Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus")
STAMP = "2026-10-18T12:00:00Z"


def sag(identifier: int, *, title: str = "Official consultation", statusid: int | None = None) -> dict[str, object]:
    return {"id": identifier, "nummer": str(identifier), "titel": title, "typeid": None, "kategoriid": None,
            "statusid": statusid, "opdateringsdato": STAMP, "retsinformationsurl": None}


def stage(identifier: int, sagid: int) -> dict[str, object]:
    return {"id": identifier, "sagid": sagid, "titel": "Stage", "dato": None, "typeid": None, "statusid": None,
            "opdateringsdato": STAMP}


def link(identifier: int, sagid: int, dokumentid: int) -> dict[str, object]:
    return {"id": identifier, "sagid": sagid, "dokumentid": dokumentid, "bilagsnummer": None,
            "frigivelsesdato": None, "rolleid": None, "opdateringsdato": STAMP}


def document(identifier: int) -> dict[str, object]:
    return {"id": identifier, "titel": "Document title", "dato": None, "modtagelsesdato": None,
            "frigivelsesdato": None, "opdateringsdato": STAMP}


def vote(identifier: int, sagstrinid: int) -> dict[str, object]:
    return {"id": identifier, "sagstrinid": sagstrinid, "nummer": 1, "vedtaget": True, "konklusion": None,
            "kommentar": None, "mødeid": None, "typeid": None, "opdateringsdato": STAMP}


def status(identifier: int) -> dict[str, object]:
    return {"id": identifier, "status": "Active", "opdateringsdato": STAMP}


def canonical_key(entity: str, identifier: int) -> str:
    return json.dumps({"entity": entity, "key": [["id", "int", identifier]]}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


def make_manifest(path: pathlib.Path, rows: dict[str, list[tuple[str, int, int]]], *, backup: str) -> None:
    total = sum(len(items) for items in rows.values())
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
                   (backup * 64, "2026-10-18T02:00:00Z", "a" * 64, "2026-10-18T03:00:00Z", total))
        for entity in ENTITIES:
            entity_rows = rows.get(entity, [])
            db.execute("INSERT INTO entity_counts VALUES (?, ?)", (entity, len(entity_rows)))
            db.executemany('INSERT INTO "rows" VALUES (?, ?, ?, ?)',
                           ((entity, key, bytes([full]) * 32, bytes([semantic]) * 32) for key, full, semantic in entity_rows))


class CandidateExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.old = self.root / "old.sqlite"
        self.new = self.root / "new.sqlite"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, changes: list[tuple[str, str, int]], source_rows: dict[str, list[dict[str, object]]]):
        new_rows: dict[str, list[tuple[str, int, int]]] = {entity: [] for entity in ENTITIES}
        old_rows: dict[str, list[tuple[str, int, int]]] = {entity: [] for entity in ENTITIES}
        for entity, category, identifier in changes:
            key = canonical_key(entity, identifier)
            if category == "added":
                new_rows[entity].append((key, 1, 1))
            elif category == "changed":
                old_rows[entity].append((key, 1, 1))
                # Deliberately hold full_hash equal to prove semantic changes are included directly.
                new_rows[entity].append((key, 1, 2))
            elif category == "technical_only":
                old_rows[entity].append((key, 1, 1))
                new_rows[entity].append((key, 2, 1))
            elif category == "unchanged":
                old_rows[entity].append((key, 1, 1))
                new_rows[entity].append((key, 1, 1))
            elif category == "removed":
                old_rows[entity].append((key, 1, 1))
        self.old.unlink(missing_ok=True)
        self.new.unlink(missing_ok=True)
        make_manifest(self.old, old_rows, backup="1")
        make_manifest(self.new, new_rows, backup="2")
        provider = CANDIDATES.InMemoryFolketingetProvider(source_rows)
        return CANDIDATES.extract_candidates(self.old, self.new, provider)

    def test_direct_sag_addition_produces_classifier_compatible_case(self) -> None:
        title = "Fordrevne fra Ukraine"
        bundle = self.build([("Sag", "added", 10)], {"Sag": [sag(10, title=title)]})
        self.assertTrue(bundle["complete"])
        case = bundle["cases"][0]
        self.assertEqual(case["source"], "folketinget")
        self.assertFalse(case["historical"])
        self.assertIsNone(case["notify_eligible"])
        self.assertEqual(classify(case).relevance, DIRECT)
        changed = self.build([("Sag", "changed", 10)], {"Sag": [sag(10, title=title)]})
        self.assertEqual(changed["candidate_case_count"], 1)

    def test_sagstrin_and_sagdokument_resolve_to_their_case(self) -> None:
        stage_bundle = self.build(
            [("Sagstrin", "added", 20)],
            {"Sag": [sag(2)], "Sagstrin": [stage(20, 2)]},
        )
        self.assertEqual([case["external_id"] for case in stage_bundle["cases"]], ["2"])
        link_bundle = self.build(
            [("SagDokument", "added", 30)],
            {"Sag": [sag(3)], "SagDokument": [link(30, 3, 300)], "Dokument": [document(300)]},
        )
        self.assertEqual([case["external_id"] for case in link_bundle["cases"]], ["3"])

    def test_document_reverse_lookup_and_vote_stage_chain(self) -> None:
        doc_bundle = self.build(
            [("Dokument", "added", 300)],
            {"Sag": [sag(4)], "SagDokument": [link(40, 4, 300)], "Dokument": [document(300)]},
        )
        self.assertEqual([case["external_id"] for case in doc_bundle["cases"]], ["4"])
        vote_bundle = self.build(
            [("Afstemning", "added", 500)],
            {"Sag": [sag(5)], "Sagstrin": [stage(400, 5)], "Afstemning": [vote(500, 400)]},
        )
        self.assertEqual([case["external_id"] for case in vote_bundle["cases"]], ["5"])

    def test_status_change_resolves_all_matching_cases_and_duplicates_collapse(self) -> None:
        bundle = self.build(
            [("Sagsstatus", "changed", 7), ("Sagstrin", "added", 20)],
            {"Sagsstatus": [status(7)], "Sag": [sag(12, statusid=7), sag(13, statusid=7)],
             "Sagstrin": [stage(20, 12)]},
        )
        self.assertEqual([case["external_id"] for case in bundle["cases"]], ["12", "13"])
        self.assertEqual(bundle["candidate_case_count"], 2)

    def test_technical_only_and_unchanged_do_not_create_candidates(self) -> None:
        bundle = self.build([("Sag", "technical_only", 1), ("Sag", "unchanged", 2)], {"Sag": []})
        self.assertEqual(bundle["candidate_case_count"], 0)
        self.assertEqual(bundle["cases"], [])
        self.assertTrue(bundle["complete"])

    def test_internal_iterator_exposes_change_keys_without_changing_public_aggregate_shape(self) -> None:
        self.build([("Sag", "added", 1), ("Sagstrin", "changed", 2),
                    ("Dokument", "technical_only", 3), ("Sagsstatus", "removed", 4)], {})
        report = CANDIDATES.snapshot_diff.compare_manifests(self.old, self.new)
        self.assertEqual(set(report), {"manifest_format_version", "old", "new", "schema_fingerprint", "entities", "totals", "validation"})
        changes = list(CANDIDATES.snapshot_diff._iter_manifest_changes(self.old, self.new))
        self.assertEqual(Counter(category for _, category, _ in changes), {
            "added": 1, "changed": 1, "technical_only": 1, "removed": 1,
        })
        self.assertTrue(all(isinstance(key, str) for _, _, key in changes))

    def test_removed_row_is_reported_and_makes_bundle_incomplete(self) -> None:
        bundle = self.build([("SagDokument", "removed", 33)], {})
        self.assertFalse(bundle["complete"])
        self.assertEqual(bundle["cases"], [])
        self.assertIn("removed_change_requires_old_snapshot_hydration", {item["code"] for item in bundle["diagnostics"]})
        summary = CANDIDATES.candidate_summary(bundle)
        self.assertEqual(summary["diff"]["totals"]["removed"], 1)

    def test_malformed_canonical_key_is_rejected_without_disclosing_it(self) -> None:
        make_manifest(self.old, {entity: [] for entity in ENTITIES}, backup="1")
        rows = {entity: [] for entity in ENTITIES}
        rows["Sag"].append(("PRIVATE-MALFORMED-KEY", 1, 1))
        make_manifest(self.new, rows, backup="2")
        provider = CANDIDATES.InMemoryFolketingetProvider({"Sag": [sag(1)]})
        with self.assertRaises(CANDIDATES.CandidateExtractionError) as raised:
            CANDIDATES.extract_candidates(self.old, self.new, provider)
        self.assertEqual(raised.exception.category, "canonical_key_invalid")
        self.assertNotIn("PRIVATE-MALFORMED-KEY", str(raised.exception))

    def test_missing_relation_fails_closed(self) -> None:
        bundle = self.build([("Sagstrin", "changed", 44)], {"Sagstrin": [stage(44, 999)]})
        self.assertFalse(bundle["complete"])
        self.assertIn("affected_case_missing", {item["code"] for item in bundle["diagnostics"]})

    def test_existing_normalization_diagnostic_is_counted_once(self) -> None:
        incomplete_sag = sag(77)
        incomplete_sag["opdateringsdato"] = None
        bundle = self.build([("Sag", "added", 77)], {"Sag": [incomplete_sag]})
        summary = CANDIDATES.candidate_summary(bundle)
        diagnostics = {item["code"]: item["count"] for item in summary["diagnostics"]}
        self.assertEqual(diagnostics["missing_timestamp"], 1)
        self.assertFalse(bundle["complete"])

    def test_case_order_is_deterministic_and_summary_does_not_leak_payload(self) -> None:
        title = "PRIVATE TITLE displaced people from Ukraine"
        bundle = self.build([("Sag", "added", 10), ("Sag", "added", 2)],
                            {"Sag": [sag(10, title=title), sag(2, title=title)]})
        self.assertEqual([case["external_id"] for case in bundle["cases"]], ["10", "2"])
        summary = json.dumps(CANDIDATES.candidate_summary(bundle), ensure_ascii=False)
        self.assertNotIn("PRIVATE TITLE", summary)
        self.assertNotIn('"external_id"', summary)
        self.assertNotIn('"id"', summary)

    def test_affected_case_limit_fails_closed(self) -> None:
        changes = [("Sag", "added", identifier) for identifier in range(1, 5002)]
        rows = {"Sag": [sag(identifier) for identifier in range(1, 5002)]}
        with self.assertRaises(CANDIDATES.CandidateExtractionError) as raised:
            self.build(changes, rows)
        self.assertEqual(raised.exception.category, "affected_case_limit_exceeded")


if __name__ == "__main__":
    unittest.main()
