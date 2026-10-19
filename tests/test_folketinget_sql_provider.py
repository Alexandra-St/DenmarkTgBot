from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import unittest
import base64
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "folketinget_sql_provider_test", ROOT / "tools" / "folketinget_sql_provider.py"
)
PROVIDER_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PROVIDER_MODULE)

POC_SPEC = importlib.util.spec_from_file_location(
    "folketinget_sql_provider_poc_test", ROOT / "tools" / "folketinget_backup_poc.py"
)
POC = importlib.util.module_from_spec(POC_SPEC)
assert POC_SPEC.loader is not None
POC_SPEC.loader.exec_module(POC)

ENTITIES = PROVIDER_MODULE.REQUIRED_ENTITIES
SCHEMAS = {entity: ("dbo", "USER_TABLE") for entity in ENTITIES}


def row(entity: str, identifier: int, **values: object) -> dict[str, object]:
    result = {field: None for field in PROVIDER_MODULE.PROJECTIONS[entity]}
    result.update({"id": identifier, "opdateringsdato": "2026-10-19T10:00:00Z"})
    result.update(values)
    return result


class SQLServerProviderTests(unittest.TestCase):
    def make_provider(self, source_rows: dict[str, list[dict[str, object]]]):
        queries: list[tuple[str, str]] = []

        def execute(query: str, entity: str) -> str:
            queries.append((query, entity))
            match = re.search(r"WHERE \[([A-Za-z]+)\] IN \(([-0-9,]+)\)", query)
            if match is None:
                raise AssertionError("expected bounded IN query")
            field = match.group(1)
            values = {int(value, 10) for value in match.group(2).split(",")}
            selected = [item for item in source_rows.get(entity, []) if item.get(field) in values]
            selected.sort(key=lambda item: item["id"])
            return json.dumps(selected, ensure_ascii=False)

        return PROVIDER_MODULE.SQLServerFolketingetProvider(SCHEMAS, execute), queries

    def test_get_by_ids_returns_exact_projection_in_deterministic_order(self) -> None:
        source = {"Sag": [row("Sag", 3), row("Sag", 1), row("Sag", 2)]}
        provider, _ = self.make_provider(source)
        results = provider.get_by_ids("Sag", [3, 1, 2, 1])
        self.assertEqual([item["id"] for item in results], [1, 2, 3])
        self.assertEqual(set(results[0]), set(PROVIDER_MODULE.PROJECTIONS["Sag"]))

    def test_find_by_field_returns_all_rows_across_bounded_batches(self) -> None:
        source = {"Sagstrin": [row("Sagstrin", identifier + 1000, sagid=identifier) for identifier in range(501)]}
        provider, queries = self.make_provider(source)
        results = provider.find_by_field("Sagstrin", "sagid", list(range(501)))
        self.assertEqual(len(results), 501)
        self.assertEqual([item["id"] for item in results], sorted(item["id"] for item in results))
        self.assertEqual(len(queries), 2)
        self.assertLessEqual(max(len(re.search(r"IN \(([^)]*)\)", query).group(1).split(",")) for query, _ in queries), 500)

    def test_get_by_ids_splits_more_than_500_and_merges_sorted(self) -> None:
        source = {"Sag": [row("Sag", identifier) for identifier in range(1, 1202)]}
        provider, queries = self.make_provider(source)
        results = provider.get_by_ids("Sag", list(range(1201, 0, -1)))
        self.assertEqual([item["id"] for item in results], list(range(1, 1202)))
        self.assertEqual(len(queries), 3)

    def test_sql_uses_only_allowlisted_projection_and_chunk_transport(self) -> None:
        provider, queries = self.make_provider({"Afstemning": [row("Afstemning", 8, sagstrinid=7)]})
        provider.find_by_field("Afstemning", "sagstrinid", [7])
        query, entity = queries[0]
        self.assertEqual(entity, "Afstemning")
        for field in PROVIDER_MODULE.PROJECTIONS["Afstemning"]:
            self.assertIn(f"[{field}] AS [{field}]", query)
        self.assertIn("FOR JSON PATH, INCLUDE_NULL_VALUES", query)
        self.assertIn("BASE64_ENCODE(CONVERT(varbinary(max), @provider_json))", query)
        self.assertIn("varchar(6000)", query)
        self.assertIn("ORDER BY sequence_number", query)
        output = "1|" + base64.b64encode("[]".encode("utf-16-le")).decode("ascii")
        with mock.patch.object(POC, "run_manifest_page_sql", return_value=output) as run:
            self.assertEqual(POC.run_provider_chunked_json("private", "SELECT 1", "Afstemning"), "[]")
        argv = run.call_args.args[0]
        self.assertIn("-h", argv)
        self.assertEqual(argv[argv.index("-h") + 1], "-1")
        self.assertIn("-W", argv)
        self.assertNotIn("-y", argv)
        self.assertEqual(argv[argv.index("-w") + 1], "6100")

    def test_all_six_projections_match_the_existing_model_contract(self) -> None:
        self.assertEqual(PROVIDER_MODULE.PROJECTIONS, {
            "Sag": ("id", "nummer", "titel", "typeid", "kategoriid", "statusid", "opdateringsdato", "retsinformationsurl"),
            "Sagstrin": ("id", "sagid", "titel", "dato", "typeid", "statusid", "opdateringsdato"),
            "SagDokument": ("id", "sagid", "dokumentid", "bilagsnummer", "frigivelsesdato", "rolleid", "opdateringsdato"),
            "Dokument": ("id", "titel", "dato", "modtagelsesdato", "frigivelsesdato", "opdateringsdato"),
            "Afstemning": ("id", "sagstrinid", "nummer", "vedtaget", "konklusion", "kommentar", "mødeid", "typeid", "opdateringsdato"),
            "Sagsstatus": ("id", "status", "opdateringsdato"),
        })

    def test_legacy_text_columns_are_cast_before_for_json(self) -> None:
        types = {entity: {} for entity in ENTITIES}
        types["Dokument"] = {"titel": "text", "dato": "ntext"}
        queries = []
        provider = PROVIDER_MODULE.SQLServerFolketingetProvider(
            SCHEMAS, lambda query, entity: queries.append(query) or "[]", column_types=types
        )
        provider.get_by_ids("Dokument", [5])
        self.assertIn("CONVERT(varchar(max), [titel]) AS [titel]", queries[0])
        self.assertIn("CONVERT(nvarchar(max), [dato]) AS [dato]", queries[0])

    def test_entity_field_and_values_are_rejected_before_query(self) -> None:
        calls = []
        provider = PROVIDER_MODULE.SQLServerFolketingetProvider(SCHEMAS, lambda query, entity: calls.append(query) or "[]")
        checks = [
            lambda: provider.get_by_ids("UserTable", [1]),
            lambda: provider.find_by_field("Sag", "titel", [1]),
            lambda: provider.get_by_ids("Sag", [True]),
            lambda: provider.find_by_field("Sagstrin", "sagid", [1.5]),
            lambda: provider.find_by_field("Sagstrin", "id", [1]),
        ]
        for call in checks:
            with self.subTest(call=call), self.assertRaises(PROVIDER_MODULE.SQLProviderError):
                call()
        self.assertEqual(calls, [])

    def test_unexpected_rows_fail_closed(self) -> None:
        provider = PROVIDER_MODULE.SQLServerFolketingetProvider(
            SCHEMAS, lambda query, entity: json.dumps([row(entity, 99)])
        )
        with self.assertRaises(PROVIDER_MODULE.SQLProviderError) as raised:
            provider.get_by_ids("Sag", [1])
        self.assertEqual(raised.exception.category, "provider_unexpected_row")

    def test_identical_duplicate_rows_are_deduplicated_and_conflicts_rejected(self) -> None:
        same = row("Sag", 1)
        provider = PROVIDER_MODULE.SQLServerFolketingetProvider(
            SCHEMAS, lambda query, entity: json.dumps([same, same])
        )
        self.assertEqual(len(provider.get_by_ids("Sag", [1])), 1)

        conflicting = row("Sag", 1, titel="private source text")
        provider = PROVIDER_MODULE.SQLServerFolketingetProvider(
            SCHEMAS, lambda query, entity: json.dumps([same, conflicting])
        )
        with self.assertRaises(PROVIDER_MODULE.SQLProviderError) as raised:
            provider.get_by_ids("Sag", [1])
        self.assertEqual(raised.exception.category, "provider_duplicate_conflict")
        self.assertNotIn("private source text", str(raised.exception))

    def test_malformed_provider_output_fails_without_payload_leakage(self) -> None:
        provider = PROVIDER_MODULE.SQLServerFolketingetProvider(
            SCHEMAS, lambda query, entity: '{"private":"row-content"}'
        )
        with self.assertRaises(PROVIDER_MODULE.SQLProviderError) as raised:
            provider.get_by_ids("Sag", [1])
        self.assertEqual(raised.exception.category, "provider_json_invalid")
        self.assertNotIn("row-content", str(raised.exception))

    def test_query_failure_message_is_sanitized(self) -> None:
        def fail(query, entity):
            raise RuntimeError("PRIVATE SQL text and row contents")

        provider = PROVIDER_MODULE.SQLServerFolketingetProvider(SCHEMAS, fail)
        with self.assertRaises(PROVIDER_MODULE.SQLProviderError) as raised:
            provider.get_by_ids("Sag", [1])
        self.assertEqual(raised.exception.category, "provider_query_failed")
        self.assertNotIn("PRIVATE", str(raised.exception))

    def test_wrong_shape_and_non_integer_identity_fail_closed(self) -> None:
        for payload in (
            json.dumps([{"id": 1, "private": "value"}]),
            json.dumps([row("Sag", "not-an-id")]),
        ):
            provider = PROVIDER_MODULE.SQLServerFolketingetProvider(SCHEMAS, lambda query, entity, p=payload: p)
            with self.assertRaises(PROVIDER_MODULE.SQLProviderError):
                provider.get_by_ids("Sag", [1])

    def test_empty_lookup_does_not_run_sql(self) -> None:
        calls = []
        provider = PROVIDER_MODULE.SQLServerFolketingetProvider(SCHEMAS, lambda query, entity: calls.append(query) or "[]")
        self.assertEqual(provider.get_by_ids("Sag", []), ())
        self.assertEqual(calls, [])

    def test_provider_smoke_exercises_by_id_relations_and_normalizers(self) -> None:
        import importlib.util

        candidate_spec = importlib.util.spec_from_file_location(
            "folketinget_candidate_extraction_smoke_test", ROOT / "tools" / "folketinget_candidate_extraction.py"
        )
        candidates = importlib.util.module_from_spec(candidate_spec)
        assert candidate_spec.loader is not None
        candidate_spec.loader.exec_module(candidates)

        fixture = {
            "Sag": [row("Sag", 1, nummer="L 1", titel="test", statusid=6)],
            "Sagstrin": [row("Sagstrin", 2, sagid=1, titel="test")],
            "SagDokument": [row("SagDokument", 3, sagid=1, dokumentid=4)],
            "Dokument": [row("Dokument", 4, titel="test")],
            "Afstemning": [row("Afstemning", 5, sagstrinid=2, nummer=1, vedtaget=True)],
            "Sagsstatus": [row("Sagsstatus", 6, status="aktiv")],
        }

        class FixtureProvider:
            def __init__(self):
                self.lookups = []

            def get_by_ids(self, entity, ids):
                return tuple(item for item in fixture[entity] if item["id"] in ids)

            def find_by_field(self, entity, field, values):
                self.lookups.append((entity, field))
                return tuple(item for item in fixture[entity] if item.get(field) in values)

        provider = FixtureProvider()
        result = POC.exercise_sql_provider(provider, {entity: fixture[entity][0]["id"] for entity in ENTITIES})
        self.assertEqual(result["entities_tested"], 6)
        self.assertEqual(result["get_by_ids_checks"], 6)
        self.assertEqual(result["normalization_failures"], 0)
        self.assertGreaterEqual(result["relation_checks"], 5)
        self.assertEqual(len(provider.lookups), result["relation_checks"])

    def test_workflow_mode_is_manual_and_nonproduction(self) -> None:
        workflow = (ROOT / ".github/workflows/folketinget-backup-poc.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn("- VALIDATE_SQL_PROVIDER", workflow)
        self.assertIn("if: inputs.mode != 'DIFF_MANIFESTS'", workflow)
        for forbidden in ("workers.dev", "api.telegram.org", "ingestion endpoint"):
            self.assertNotIn(forbidden, workflow.lower())


if __name__ == "__main__":
    unittest.main()
