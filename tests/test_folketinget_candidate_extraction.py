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
SQLProviderError = CANDIDATES.SQLProviderError

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


def make_manifest(path: pathlib.Path, rows: dict[str, list[tuple[str, int, int]]], *, backup: str,
                  routing: dict[tuple[str, int], dict[str, int | None]] | None = None, format_version: int = 2) -> None:
    total = sum(len(items) for items in rows.values())
    routing = routing or {}
    with sqlite3.connect(path) as db:
        metadata_columns = ("manifest_format_version INTEGER NOT NULL, routing_row_count INTEGER NOT NULL") if format_version == 2 else ""
        db.executescript(
            "CREATE TABLE snapshot_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), backup_sha256 TEXT NOT NULL, "
            "backup_finish_date TEXT NOT NULL, source_sql_major INTEGER NOT NULL, schema_fingerprint TEXT NOT NULL, "
            f"generated_at TEXT NOT NULL, total_rows INTEGER NOT NULL{', ' + metadata_columns if metadata_columns else ''});"
            "CREATE TABLE entity_counts (entity TEXT PRIMARY KEY, row_count INTEGER NOT NULL) WITHOUT ROWID;"
            'CREATE TABLE "rows" (entity TEXT NOT NULL, canonical_key TEXT NOT NULL, full_hash BLOB NOT NULL CHECK(length(full_hash)=32), '
            "semantic_hash BLOB NOT NULL CHECK(length(semantic_hash)=32), PRIMARY KEY(entity, canonical_key)) WITHOUT ROWID;"
            + ("CREATE TABLE routing (entity TEXT NOT NULL,row_id INTEGER NOT NULL,sagid INTEGER,dokumentid INTEGER,sagstrinid INTEGER,statusid INTEGER,PRIMARY KEY(entity,row_id)) WITHOUT ROWID;"
               "CREATE INDEX routing_sagid_idx ON routing(sagid) WHERE sagid IS NOT NULL;"
               "CREATE INDEX routing_dokumentid_idx ON routing(dokumentid) WHERE entity='SagDokument' AND dokumentid IS NOT NULL;"
               "CREATE INDEX routing_sagstrinid_idx ON routing(sagstrinid) WHERE entity='Afstemning' AND sagstrinid IS NOT NULL;"
               "CREATE INDEX routing_statusid_idx ON routing(statusid) WHERE entity='Sag' AND statusid IS NOT NULL;" if format_version == 2 else "")
        )
        if format_version == 2:
            db.execute("INSERT INTO snapshot_metadata VALUES (1, ?, ?, 17, ?, ?, ?, 2, ?)",
                       (backup * 64, "2026-10-18T02:00:00Z", "a" * 64, "2026-10-18T03:00:00Z", total, total))
        else:
            db.execute("INSERT INTO snapshot_metadata VALUES (1, ?, ?, 17, ?, ?, ?)",
                       (backup * 64, "2026-10-18T02:00:00Z", "a" * 64, "2026-10-18T03:00:00Z", total))
        for entity in ENTITIES:
            entity_rows = rows.get(entity, [])
            db.execute("INSERT INTO entity_counts VALUES (?, ?)", (entity, len(entity_rows)))
            db.executemany('INSERT INTO "rows" VALUES (?, ?, ?, ?)',
                           ((entity, key, bytes([full]) * 32, bytes([semantic]) * 32) for key, full, semantic in entity_rows))
            if format_version == 2:
                for key, _full, _semantic in entity_rows:
                    try:
                        identifier = json.loads(key)["key"][0][2]
                    except (ValueError, KeyError, IndexError, TypeError):
                        continue
                    values = routing.get((entity, identifier), {})
                    defaults = {"sagid": identifier, "dokumentid": identifier, "sagstrinid": identifier, "statusid": identifier}
                    defaults.update(values)
                    db.execute("INSERT INTO routing VALUES (?,?,?,?,?,?)",
                               (entity, identifier, defaults.get("sagid") if entity in {"Sagstrin", "SagDokument"} else None,
                                defaults.get("dokumentid") if entity == "SagDokument" else None,
                                defaults.get("sagstrinid") if entity == "Afstemning" else None,
                                defaults.get("statusid") if entity == "Sag" else None))


class CandidateExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.old = self.root / "old.sqlite"
        self.new = self.root / "new.sqlite"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_unresolved_relationship_diagnostics_are_specific_and_aggregate(self) -> None:
        cases = (
            ("Sagstrin", 10, {"Sagstrin": [{"id": 10, "sagid": None}]}, "sagstrin_invalid_sagid"),
            ("SagDokument", 11, {"SagDokument": [{"id": 11, "sagid": "bad"}]}, "sagdokument_invalid_sagid"),
            ("Dokument", 12, {"Dokument": [{"id": 12}], "SagDokument": []}, "document_no_links"),
            ("Dokument", 13, {"Dokument": [{"id": 13}], "SagDokument": [{"id": 20, "dokumentid": 13, "sagid": None}]}, "document_link_invalid_sagid"),
            ("Afstemning", 14, {"Afstemning": [{"id": 14, "sagstrinid": None}]}, "vote_invalid_stageid"),
            ("Afstemning", 15, {"Afstemning": [{"id": 15, "sagstrinid": 99}], "Sagstrin": []}, "vote_stage_not_found"),
            ("Afstemning", 16, {"Afstemning": [{"id": 16, "sagstrinid": 100}], "Sagstrin": [{"id": 100, "sagid": None}]}, "vote_stage_invalid_sagid"),
            ("Sagsstatus", 17, {"Sagsstatus": [{"id": 17}], "Sag": []}, "status_no_matching_sags"),
            ("Sagsstatus", 18, {"Sagsstatus": [{"id": 18}], "Sag": [{"statusid": 18, "id": None}]}, "status_invalid_sag_id"),
        )
        totals = Counter()
        for entity, identifier, rows, expected in cases:
            with self.subTest(code=expected):
                provider = CANDIDATES.InMemoryFolketingetProvider(rows)
                diagnostics = Counter()
                CANDIDATES._resolve_affected_sag_ids(entity, identifier, provider, diagnostics)
                self.assertEqual(diagnostics["unresolved_relationship"], 1)
                self.assertEqual(diagnostics[expected], 1)
                totals.update(diagnostics)

        specific_codes = {expected for _entity, _identifier, _rows, expected in cases}
        self.assertEqual(totals["unresolved_relationship"], sum(totals[code] for code in specific_codes))
        self.assertEqual(sum(totals[code] for code in specific_codes), len(cases))

        bundle = self.build([("Sagstrin", "added", 19)], {"Sagstrin": [{"id": 19, "sagid": None}]})
        summary_diagnostics = {
            item["code"]: item["count"] for item in CANDIDATES.candidate_summary(bundle)["diagnostics"]
        }
        self.assertEqual(summary_diagnostics["unresolved_relationship"], 1)
        self.assertEqual(summary_diagnostics["sagstrin_invalid_sagid"], 1)

    def build(self, changes: list[tuple[str, str, int]], source_rows: dict[str, list[dict[str, object]]], *,
              old_routing: dict[tuple[str, int], dict[str, int | None]] | None = None, old_format: int = 2,
              max_affected_cases: int = CANDIDATES.MAX_AFFECTED_CASES):
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
        make_manifest(self.old, old_rows, backup="1", routing=old_routing, format_version=old_format)
        make_manifest(self.new, new_rows, backup="2")
        provider = CANDIDATES.InMemoryFolketingetProvider(source_rows)
        return CANDIDATES.extract_candidates(self.old, self.new, provider, max_affected_cases=max_affected_cases)

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
        self.assertNotIn("removed_change_requires_old_snapshot_hydration", {item["code"] for item in bundle["diagnostics"]})
        self.assertIn("affected_case_missing", {item["code"] for item in bundle["diagnostics"]})
        summary = CANDIDATES.candidate_summary(bundle)
        self.assertEqual(summary["diff"]["totals"]["removed"], 1)

    def test_removed_sag_becomes_tombstone_without_hydration(self) -> None:
        bundle = self.build([("Sag", "removed", 10)], {})
        self.assertTrue(bundle["complete"])
        self.assertEqual(bundle["_summary"]["affected_case_count"], 1)
        self.assertEqual(bundle["cases"], [])
        self.assertEqual(bundle["tombstones"], [{"operation": "SOURCE_DELETE", "source": "folketinget", "external_id": "10"}])
        self.assertNotIn("affected_case_missing", {item["code"] for item in bundle["diagnostics"]})

    def test_removed_sagstrin_and_sagdokument_use_old_sagid(self) -> None:
        for entity, source in (("Sagstrin", {"Sag": [sag(10)], "Sagstrin": [stage(99, 10)]}),
                              ("SagDokument", {"Sag": [sag(10)]})):
            bundle = self.build([(entity, "removed", 33)], source,
                                old_routing={(entity, 33): {"sagid": 10}})
            self.assertTrue(bundle["complete"], entity)
            self.assertEqual(bundle["_summary"]["affected_case_count"], 1)
            self.assertEqual(bundle["cases"][0]["external_id"], "10")

    def test_removed_document_routes_through_old_sagdokument(self) -> None:
        bundle = self.build([("Dokument", "removed", 20), ("SagDokument", "removed", 30)],
                            {"Sag": [sag(10)]},
                            old_routing={("Dokument", 20): {}, ("SagDokument", 30): {"sagid": 10, "dokumentid": 20}})
        self.assertTrue(bundle["complete"])
        self.assertEqual(bundle["_summary"]["affected_case_count"], 1)

    def test_removed_vote_routes_through_old_stage(self) -> None:
        bundle = self.build([("Afstemning", "removed", 40), ("Sagstrin", "unchanged", 50)],
                            {"Sag": [sag(10)], "Sagstrin": [stage(50, 10)]},
                            old_routing={("Afstemning", 40): {"sagstrinid": 50}, ("Sagstrin", 50): {"sagid": 10}})
        self.assertTrue(bundle["complete"])
        self.assertEqual(bundle["_summary"]["affected_case_count"], 1)

    def test_removed_status_routes_to_all_old_sags_and_deduplicates(self) -> None:
        bundle = self.build([("Sagsstatus", "removed", 60), ("Sag", "changed", 10)],
                            {"Sag": [sag(10)]},
                            old_routing={("Sagsstatus", 60): {}, ("Sag", 10): {"statusid": 60}})
        self.assertTrue(bundle["complete"])
        self.assertEqual(bundle["_summary"]["affected_case_count"], 1)

    def test_v1_removed_manifest_remains_incomplete(self) -> None:
        bundle = self.build([("Sagstrin", "removed", 33)], {"Sag": [sag(10)]}, old_format=1)
        self.assertFalse(bundle["complete"])
        self.assertIn("removed_change_requires_old_snapshot_hydration", {d["code"] for d in bundle["diagnostics"]})

    def test_removed_and_current_changes_deduplicate_before_case_limit(self) -> None:
        bundle = self.build([("Sag", "added", 10), ("Sagstrin", "changed", 22), ("Sagstrin", "removed", 33)],
                            {"Sag": [sag(10)], "Sagstrin": [stage(22, 10)]},
                            old_routing={("Sagstrin", 33): {"sagid": 10}})
        self.assertEqual(bundle["_summary"]["affected_case_count"], 1)
        confirmed_delete = self.build([("Sag", "removed", 10), ("Sagstrin", "removed", 33)], {},
                                      old_routing={("Sagstrin", 33): {"sagid": 10}})
        self.assertTrue(confirmed_delete["complete"])
        self.assertEqual(confirmed_delete["cases"], [])
        self.assertEqual(confirmed_delete["_summary"]["tombstone_count"], 1)
        unexpected_missing = self.build([("Sagstrin", "removed", 34)], {},
                                        old_routing={("Sagstrin", 34): {"sagid": 11}})
        self.assertFalse(unexpected_missing["complete"])
        self.assertIn("affected_case_missing", {item["code"] for item in unexpected_missing["diagnostics"]})
        with self.assertRaises(CANDIDATES.CandidateExtractionError) as raised:
            self.build([("Sagstrin", "removed", 31), ("Sagstrin", "removed", 32), ("Sagstrin", "removed", 33)],
                       {"Sag": [sag(10), sag(11), sag(12)]},
                       old_routing={("Sagstrin", 31): {"sagid": 10}, ("Sagstrin", 32): {"sagid": 11},
                                    ("Sagstrin", 33): {"sagid": 12}}, max_affected_cases=2)
        self.assertEqual(raised.exception.category, "affected_case_limit_exceeded")

    def test_malformed_canonical_key_is_rejected_without_disclosing_it(self) -> None:
        make_manifest(self.old, {entity: [] for entity in ENTITIES}, backup="1")
        rows = {entity: [] for entity in ENTITIES}
        rows["Sag"].append(("PRIVATE-MALFORMED-KEY", 1, 1))
        make_manifest(self.new, rows, backup="2", format_version=1)
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

    def test_provider_lookup_diagnostics_preserve_allowlisted_dimensions(self) -> None:
        class FailingProvider:
            def __init__(self, error):
                self.error = error

            def get_by_ids(self, entity, ids):
                raise self.error

            def find_by_field(self, entity, field, values):
                raise self.error

        cases = (
            ("by_ids", "Dokument", "id", SQLProviderError("provider_json_invalid"),
             "source_lookup_failed_Dokument_by_ids_id_provider_json_invalid"),
            ("by_field", "SagDokument", "sagid", SQLProviderError("provider_query_failed"),
             "source_lookup_failed_SagDokument_by_field_sagid_provider_query_failed"),
            ("by_field", "Afstemning", "sagstrinid",
             SQLProviderError("provider_relation_field_type_invalid"),
             "source_lookup_failed_Afstemning_by_field_sagstrinid_provider_relation_field_type_invalid"),
            ("by_field", "SagDokument", "sagid",
             SQLProviderError("provider_transport_failed", transport_category="manifest_page_json_invalid"),
             "source_lookup_failed_SagDokument_by_field_sagid_provider_transport_failed_transport_manifest_page_json_invalid"),
            ("by_field", "SagDokument", "sagid",
             SQLProviderError("provider_transport_failed", transport_category="PRIVATE SQL row 123"),
             "source_lookup_failed_SagDokument_by_field_sagid_provider_transport_failed_transport_provider_transport_failed"),
        )
        for method, entity, field, error, expected_code in cases:
            with self.subTest(code=expected_code):
                diagnostics = Counter()
                rows = CANDIDATES._safe_lookup(
                    FailingProvider(error), method, entity, (987654,), diagnostics, field=field
                )
                self.assertEqual(rows, ())
                self.assertEqual(diagnostics["source_lookup_failed"], 1)
                self.assertEqual(diagnostics[expected_code], 1)
                self.assertTrue(CANDIDATES.is_safe_diagnostic_code(expected_code))

    def test_unknown_provider_exception_and_unallowlisted_dimensions_stay_generic(self) -> None:
        class FailingProvider:
            def get_by_ids(self, entity, ids):
                raise RuntimeError("SECRET SQL title=PRIVATE id=987654 payload")

            def find_by_field(self, entity, field, values):
                raise SQLProviderError("secret_category")

        diagnostics = Counter()
        CANDIDATES._safe_lookup(FailingProvider(), "by_ids", "Sag", (987654,), diagnostics)
        self.assertEqual(diagnostics, Counter({"source_lookup_failed": 1}))
        diagnostics.clear()
        CANDIDATES._safe_lookup(
            FailingProvider(), "by_field", "SagDokument", (987654,), diagnostics, field="private_field"
        )
        self.assertEqual(diagnostics, Counter({"source_lookup_failed": 1}))
        rendered = json.dumps(dict(diagnostics))
        for value in ("SECRET", "SQL", "PRIVATE", "987654", "payload", "secret_category", "private_field"):
            self.assertNotIn(value, rendered)

    def test_persistent_adaptive_transport_failure_remains_candidate_incomplete(self) -> None:
        class PersistentProvider:
            def get_by_ids(self, entity, ids):
                if entity == "Sag":
                    return (sag(901),)
                return ()

            def find_by_field(self, entity, field, values):
                if entity == "Sagstrin" and field == "sagid":
                    raise SQLProviderError(
                        "provider_transport_failed", transport_category="manifest_page_chunk_row_invalid"
                    )
                return ()

        # A Sag addition hydrates through a singleton stage lookup that remains failed.
        old_rows = {entity: [] for entity in ENTITIES}
        new_rows = {entity: [] for entity in ENTITIES}
        new_rows["Sag"].append((canonical_key("Sag", 901), 1, 1))
        make_manifest(self.old, old_rows, backup="1")
        make_manifest(self.new, new_rows, backup="2")
        bundle = CANDIDATES.extract_candidates(self.old, self.new, PersistentProvider())
        self.assertFalse(bundle["complete"])
        diagnostics = {item["code"]: item["count"] for item in bundle["diagnostics"]}
        self.assertEqual(diagnostics["source_lookup_failed"], 1)
        self.assertEqual(
            diagnostics["source_lookup_failed_Sagstrin_by_field_sagid_provider_transport_failed_transport_manifest_page_chunk_row_invalid"],
            1,
        )

    def test_categorized_lookup_codes_are_hard_allowlisted(self) -> None:
        self.assertTrue(CANDIDATES.is_safe_diagnostic_code("source_lookup_failed"))
        self.assertTrue(CANDIDATES.is_safe_diagnostic_code(
            "source_lookup_failed_Afstemning_by_field_sagstrinid_provider_transport_failed_transport_manifest_page_sql_msg_400"
        ))
        for code in (
            "source_lookup_failed_PRIVATE_by_ids_id_provider_json_invalid",
            "source_lookup_failed_Sag_by_field_id_provider_query_failed",
            "source_lookup_failed_Sag_by_ids_id_private_category",
            "source_lookup_failed_Sag_by_ids_id_provider_query_failed_transport_private",
        ):
            self.assertFalse(CANDIDATES.is_safe_diagnostic_code(code))

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
