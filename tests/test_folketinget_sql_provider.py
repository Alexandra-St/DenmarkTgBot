from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import pathlib
import re
import subprocess
import sys
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

    def test_retryable_chunk_transport_errors_split_deterministically(self) -> None:
        retryable = (
            "manifest_page_chunk_row_invalid",
            "manifest_page_chunk_sequence_invalid",
            "manifest_page_too_large",
        )
        for category in retryable:
            with self.subTest(category=category):
                calls: list[tuple[int, ...]] = []

                def execute(query: str, entity: str) -> str:
                    match = re.search(r"WHERE \[id\] IN \(([-0-9,]+)\)", query)
                    values = tuple(int(value) for value in match.group(1).split(","))
                    calls.append(values)
                    if len(values) > 1:
                        raise PROVIDER_MODULE.SQLProviderError(
                            "provider_transport_failed", transport_category=category
                        )
                    return json.dumps([row("Sag", values[0])])

                provider = PROVIDER_MODULE.SQLServerFolketingetProvider(SCHEMAS, execute)
                results = provider.get_by_ids("Sag", [4, 3, 2, 1])
                self.assertEqual([item["id"] for item in results], [1, 2, 3, 4])
                self.assertEqual(calls, [(1, 2, 3, 4), (1, 2), (1,), (2,), (3, 4), (3,), (4,)])
                self.assertEqual(provider.adaptive_summary(), {
                    "adaptive_split_count": 3,
                    "adaptive_split_success_count": 1,
                    "adaptive_split_max_depth": 2,
                    "adaptive_terminal_failure_count": 0,
                    "adaptive_terminal_failures_by_row_shape": {},
                    "adaptive_terminal_failures_by_line_length_bucket": {},
                })

    def test_nonretryable_errors_and_single_value_failures_propagate_without_splitting(self) -> None:
        for error in (
            PROVIDER_MODULE.SQLProviderError("provider_query_failed"),
            PROVIDER_MODULE.SQLProviderError("provider_json_invalid"),
            PROVIDER_MODULE.SQLProviderError(
                "provider_transport_failed", transport_category="manifest_page_json_invalid"
            ),
        ):
            calls = []

            def execute(query, entity):
                calls.append(query)
                raise error

            provider = PROVIDER_MODULE.SQLServerFolketingetProvider(SCHEMAS, execute)
            with self.assertRaises(PROVIDER_MODULE.SQLProviderError) as raised:
                provider.get_by_ids("Sag", [1, 2])
            self.assertIs(raised.exception, error)
            self.assertEqual(len(calls), 1)
            self.assertEqual(provider.adaptive_summary()["adaptive_split_count"], 0)

        persistent = PROVIDER_MODULE.SQLProviderError(
            "provider_transport_failed", transport_category="manifest_page_chunk_row_invalid",
            transport_row_shape="null_token", transport_line_length_bucket="1-16",
            transport_separator_count_bucket="0",
        )
        calls = []

        def failing_single(query, entity):
            calls.append(query)
            raise persistent

        provider = PROVIDER_MODULE.SQLServerFolketingetProvider(SCHEMAS, failing_single)
        with self.assertRaises(PROVIDER_MODULE.SQLProviderError) as raised:
            provider.get_by_ids("Sag", [42])
        self.assertIs(raised.exception, persistent)
        self.assertEqual(len(calls), 1)
        self.assertEqual(provider.adaptive_summary(), {
            "adaptive_split_count": 0,
            "adaptive_split_success_count": 0,
            "adaptive_split_max_depth": 0,
            "adaptive_terminal_failure_count": 1,
            "adaptive_terminal_failures_by_row_shape": {"null_token": 1},
            "adaptive_terminal_failures_by_line_length_bucket": {"1-16": 1},
        })

    def test_adaptive_split_merge_preserves_duplicate_conflict_protection(self) -> None:
        def execute(query: str, entity: str) -> str:
            match = re.search(r"WHERE \[sagid\] IN \(([-0-9,]+)\)", query)
            values = tuple(int(value) for value in match.group(1).split(","))
            if len(values) > 1:
                raise PROVIDER_MODULE.SQLProviderError(
                    "provider_transport_failed", transport_category="manifest_page_too_large"
                )
            return json.dumps([row("Sagstrin", 99, sagid=values[0])])

        provider = PROVIDER_MODULE.SQLServerFolketingetProvider(SCHEMAS, execute)
        with self.assertRaises(PROVIDER_MODULE.SQLProviderError) as raised:
            provider.find_by_field("Sagstrin", "sagid", [2, 1])
        self.assertEqual(raised.exception.category, "provider_duplicate_conflict")
        self.assertEqual(provider.adaptive_summary()["adaptive_split_count"], 1)
        self.assertEqual(provider.adaptive_summary()["adaptive_split_success_count"], 0)

    def test_adaptive_recursion_is_bounded_and_summary_contains_only_aggregates(self) -> None:
        calls = 0

        def execute(query: str, entity: str) -> str:
            nonlocal calls
            calls += 1
            match = re.search(r"WHERE \[sagid\] IN \(([-0-9,]+)\)", query)
            values = tuple(int(value) for value in match.group(1).split(","))
            if len(values) > 1:
                raise PROVIDER_MODULE.SQLProviderError(
                    "provider_transport_failed", transport_category="manifest_page_chunk_row_invalid"
                )
            return "[]"

        provider = PROVIDER_MODULE.SQLServerFolketingetProvider(SCHEMAS, execute)
        requested_values = list(range(8000, 8500))
        self.assertEqual(provider.find_by_field("Sagstrin", "sagid", requested_values), ())
        metrics = provider.adaptive_summary()
        self.assertEqual(metrics["adaptive_split_count"], 499)
        self.assertEqual(metrics["adaptive_split_success_count"], 1)
        self.assertEqual(metrics["adaptive_split_max_depth"], 9)
        self.assertEqual(metrics["adaptive_terminal_failure_count"], 0)
        self.assertEqual(metrics["adaptive_terminal_failures_by_row_shape"], {})
        self.assertEqual(calls, 999)
        self.assertLessEqual(metrics["adaptive_split_max_depth"], PROVIDER_MODULE.MAX_ADAPTIVE_SPLIT_DEPTH)
        rendered = json.dumps(metrics)
        for private in ("8000", "8500", "SELECT", "payload", "Sagstrin"):
            self.assertNotIn(private, rendered)

    def test_ordinary_success_has_zero_adaptive_metrics(self) -> None:
        provider, _ = self.make_provider({"Sag": [row("Sag", 4)]})
        self.assertEqual(provider.get_by_ids("Sag", [4]), (row("Sag", 4),))
        self.assertEqual(provider.adaptive_summary(), {
            "adaptive_split_count": 0,
            "adaptive_split_success_count": 0,
            "adaptive_split_max_depth": 0,
            "adaptive_terminal_failure_count": 0,
            "adaptive_terminal_failures_by_row_shape": {},
            "adaptive_terminal_failures_by_line_length_bucket": {},
        })

    def test_sql_uses_only_allowlisted_projection_and_chunk_transport(self) -> None:
        provider, queries = self.make_provider({"Afstemning": [row("Afstemning", 8, sagstrinid=7)]})
        provider.find_by_field("Afstemning", "sagstrinid", [7])
        query, entity = queries[0]
        self.assertEqual(entity, "Afstemning")
        for field in PROVIDER_MODULE.PROJECTIONS["Afstemning"]:
            self.assertIn(f"[{field}] AS [{field}]", query)
        self.assertIn("FOR JSON PATH, INCLUDE_NULL_VALUES", query)
        self.assertIn("SET @provider_json = COALESCE((SELECT ", query)
        self.assertIn("), N'[]');", query)
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

    def test_empty_provider_lookup_uses_valid_encoded_empty_array(self) -> None:
        encoded_empty_array = "1|" + base64.b64encode("[]".encode("utf-16-le")).decode("ascii")
        provider = PROVIDER_MODULE.SQLServerFolketingetProvider(
            SCHEMAS,
            lambda query, entity: POC.run_provider_chunked_json("private", query, entity),
        )
        with mock.patch.object(POC, "run_manifest_page_sql", return_value=encoded_empty_array):
            self.assertEqual(provider.find_by_field("Afstemning", "sagstrinid", [7654321]), ())
        self.assertEqual(provider.adaptive_summary()["adaptive_terminal_failure_count"], 0)

    def test_shared_transport_imports_in_either_order_without_poc_dependency(self) -> None:
        scripts = (
            "import sys; from pathlib import Path; "
            f"sys.path.insert(0, {str(ROOT / 'tools')!r}); "
        )
        assertions = (
            "import folketinget_sql_transport as t; "
            "import folketinget_backup_poc as p; "
            "import folketinget_sql_provider as q; "
            "assert p._build_chunked_json_query is t.build_chunked_json_query; "
            "assert q.build_chunked_json_query is t.build_chunked_json_query; "
            "assert p._quote_identifier is t.quote_identifier; "
            "assert q.quote_identifier is t.quote_identifier"
        )
        reverse_assertions = (
            "import folketinget_sql_transport as t; "
            "import folketinget_sql_provider as q; "
            "assert 'folketinget_backup_poc' not in sys.modules; "
            "assert q.build_chunked_json_query is t.build_chunked_json_query; "
            "import folketinget_backup_poc as p; "
            "assert p._build_chunked_json_query is t.build_chunked_json_query; "
            "assert p._quote_identifier is t.quote_identifier"
        )
        for code in (scripts + assertions, scripts + reverse_assertions):
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 0, msg="import topology subprocess failed")

    def test_shared_transport_keeps_exact_bounded_chunk_protocol(self) -> None:
        sys.path.insert(0, str(ROOT / "tools"))
        import folketinget_sql_transport as transport

        query = transport.build_chunked_json_query("SELECT [id] FROM [dbo].[Sag] FOR JSON PATH", variable="provider")
        self.assertEqual(query, POC.build_chunked_json_query(
            "SELECT [id] FROM [dbo].[Sag] FOR JSON PATH", variable="provider"
        ))
        self.assertIn("varchar(6000)", query)
        self.assertIn("CAST(sequence_number AS varchar(5)) + '|'", query)
        self.assertIn("ORDER BY sequence_number", query)
        self.assertIn("OPTION (MAXRECURSION 30000)", query)
        self.assertIn("SET @provider_json = COALESCE((SELECT ", query)
        manifest_query = transport.build_chunked_json_query(
            "SELECT [id] FROM [dbo].[Sag] FOR JSON PATH", variable="manifest"
        )
        self.assertEqual(manifest_query, transport.build_chunked_json_query(
            "SELECT [id] FROM [dbo].[Sag] FOR JSON PATH", variable="manifest"
        ))
        self.assertIn("SET @manifest_json = COALESCE((SELECT ", manifest_query)
        self.assertIn("), N'[]');", manifest_query)

    def test_provider_validation_unexpected_sample_error_is_phase_categorized(self) -> None:
        entities = {"objects": {entity: ("dbo", "USER_TABLE") for entity in ENTITIES}}
        output = []
        with mock.patch.object(POC, "read_sql_provider_sample_ids", side_effect=RuntimeError("private failure")):
            with mock.patch("builtins.print", side_effect=lambda *args, **kwargs: output.append(args[0])):
                with self.assertRaises(POC.PocError) as raised:
                    POC.validate_sql_provider("secret", entities)
        self.assertEqual(raised.exception.category, "sql_provider_sample_validation_failed")
        self.assertEqual(output, ["progress=sql_provider_import", "progress=sql_provider_sample_ids"])
        self.assertNotIn("private", str(raised.exception))

    def test_provider_validation_reports_all_successful_phases_in_order(self) -> None:
        import folketinget_sql_provider as canonical_provider

        entities = {"objects": {entity: ("dbo", "USER_TABLE") for entity in ENTITIES}}
        columns = {
            entity: {"columns": [{"name": field, "type_name": "int"} for field in canonical_provider.PROJECTIONS[entity]]}
            for entity in ENTITIES
        }
        fixture_rows = {
            "Sag": row("Sag", 1, nummer="L 1", titel="test", statusid=6),
            "Sagstrin": row("Sagstrin", 2, sagid=1, titel="test"),
            "SagDokument": row("SagDokument", 3, sagid=1, dokumentid=4),
            "Dokument": row("Dokument", 4, titel="test"),
            "Afstemning": row("Afstemning", 5, sagstrinid=2, nummer=1, vedtaget=True),
            "Sagsstatus": row("Sagsstatus", 6, status="aktiv"),
        }
        samples = {entity: fixture_rows[entity]["id"] for entity in ENTITIES}

        class FixtureProvider:
            def __init__(self, schemas, executor, *, column_types):
                self.schemas = schemas

            def get_by_ids(self, entity, ids):
                return (fixture_rows[entity],)

            def find_by_field(self, entity, field, values):
                return ()

        output = []
        with (
            mock.patch.object(POC, "read_sql_provider_sample_ids", return_value=samples),
            mock.patch.object(POC, "read_sql_provider_relation_samples", return_value=[]),
            mock.patch.object(POC, "manifest_schema", side_effect=lambda password, entity, schema: columns[entity]),
            mock.patch.object(canonical_provider, "SQLServerFolketingetProvider", FixtureProvider),
            mock.patch("builtins.print", side_effect=lambda *args, **kwargs: output.append(args[0])),
        ):
            result = POC.validate_sql_provider("private", entities)
        self.assertEqual(result["entities_tested"], 6)
        self.assertEqual(output, [
            "progress=sql_provider_import",
            "progress=sql_provider_sample_ids",
            "progress=sql_provider_relation_samples",
            "progress=sql_provider_schema_check",
            "progress=sql_provider_construct",
            "progress=sql_provider_get_by_ids",
            *[f"progress=sql_provider_get_by_ids entity={entity}" for entity in ENTITIES],
            "progress=sql_provider_relation_checks",
        ])

    def test_provider_get_and_relation_failures_have_separate_safe_phases(self) -> None:
        sample_ids = {entity: 1 for entity in ENTITIES}
        output = []
        provider = mock.Mock()
        provider.get_by_ids.side_effect = RuntimeError("private ID and row")
        with mock.patch("builtins.print", side_effect=lambda *args, **kwargs: output.append(args[0])):
            with self.assertRaises(POC.PocError) as raised:
                POC.exercise_sql_provider(provider, sample_ids)
        self.assertEqual(raised.exception.category, "sql_provider_get_by_ids_failed")
        self.assertEqual(output, [
            "progress=sql_provider_get_by_ids",
            "progress=sql_provider_get_by_ids entity=Sag",
        ])

        provider = mock.Mock()
        provider.get_by_ids.side_effect = lambda entity, ids: (row(entity, 1),)
        provider.find_by_field.side_effect = RuntimeError("private relation row")
        output.clear()
        with mock.patch("builtins.print", side_effect=lambda *args, **kwargs: output.append(args[0])):
            with self.assertRaises(POC.PocError) as raised:
                POC.exercise_sql_provider(provider, sample_ids, [("Sag", "statusid", 2)])
        self.assertEqual(raised.exception.category, "sql_provider_relation_validation_failed")
        self.assertEqual(output[:1], ["progress=sql_provider_get_by_ids"])
        self.assertEqual(output[1:7], [f"progress=sql_provider_get_by_ids entity={entity}" for entity in ENTITIES])
        self.assertEqual(output[7:], [
            "progress=sql_provider_relation_checks",
            "progress=sql_provider_relation_check entity=Sag field=statusid",
        ])

    def test_provider_category_survives_smoke_without_logging_ids_or_payload(self) -> None:
        import folketinget_sql_provider as canonical_provider

        provider = mock.Mock()
        provider.get_by_ids.side_effect = canonical_provider.SQLProviderError("provider_row_shape_invalid")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(POC.PocError) as raised:
                POC.exercise_sql_provider(provider, {entity: 987654321 for entity in ENTITIES})
        self.assertEqual(raised.exception.category, "sql_provider_get_by_ids_provider_row_shape_invalid")
        self.assertIn("sql_provider_failure phase=get_by_ids entity=Sag category=provider_row_shape_invalid", err.getvalue())
        self.assertIn("progress=sql_provider_get_by_ids entity=Sag", out.getvalue())
        for private_value in ("987654321", "row payload", "title", "SQL"):
            self.assertNotIn(private_value, out.getvalue() + err.getvalue())

    def test_relation_provider_category_survives_smoke_with_safe_field_marker(self) -> None:
        import folketinget_sql_provider as canonical_provider

        provider = mock.Mock()
        provider.get_by_ids.side_effect = lambda entity, ids: (row(entity, ids[0]),)
        provider.find_by_field.side_effect = canonical_provider.SQLProviderError("provider_unexpected_row")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(POC.PocError) as raised:
                POC.exercise_sql_provider(provider, {entity: 1 for entity in ENTITIES}, [("Sag", "statusid", 987654321)])
        self.assertEqual(raised.exception.category, "sql_provider_relation_check_provider_unexpected_row")
        self.assertIn("progress=sql_provider_relation_check entity=Sag field=statusid", out.getvalue())
        self.assertIn("sql_provider_failure phase=relation_check entity=Sag category=provider_unexpected_row", err.getvalue())
        self.assertNotIn("987654321", out.getvalue() + err.getvalue())

    def test_transport_poc_error_category_is_retained_safely(self) -> None:
        import folketinget_sql_provider as canonical_provider

        class TransportPocError(Exception):
            category = "manifest_page_chunk_row_invalid"

        def fail(query, entity):
            raise TransportPocError("private query and payload")

        provider = canonical_provider.SQLServerFolketingetProvider(SCHEMAS, fail)
        with self.assertRaises(canonical_provider.SQLProviderError) as raised:
            provider.get_by_ids("Sag", [1])
        self.assertEqual(raised.exception.category, "provider_transport_failed")
        self.assertEqual(raised.exception.transport_category, "manifest_page_chunk_row_invalid")

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(POC.PocError) as smoke_error:
                POC.exercise_sql_provider(provider, {entity: 1 for entity in ENTITIES})
        self.assertEqual(smoke_error.exception.category, "sql_provider_get_by_ids_provider_transport_failed")
        self.assertIn("transport_category=manifest_page_chunk_row_invalid", err.getvalue())
        self.assertNotIn("private", out.getvalue() + err.getvalue())

    def test_parser_row_shape_metadata_propagates_through_provider_error_safely(self) -> None:
        import folketinget_sql_provider as canonical_provider

        provider = canonical_provider.SQLServerFolketingetProvider(
            SCHEMAS,
            lambda query, entity: POC.run_provider_chunked_json("private", query, entity),
        )
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(POC, "run_manifest_page_sql", return_value="NULL\n"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(canonical_provider.SQLProviderError) as raised:
                provider.get_by_ids("Sag", [987654])
        error = raised.exception
        self.assertEqual(error.category, "provider_transport_failed")
        self.assertEqual(error.transport_category, "manifest_page_chunk_row_invalid")
        self.assertEqual(error.transport_row_shape, "null_token")
        self.assertEqual(error.transport_line_length_bucket, "1-16")
        self.assertEqual(error.transport_separator_count_bucket, "0")
        self.assertEqual(provider.adaptive_summary()["adaptive_terminal_failure_count"], 1)
        self.assertEqual(provider.adaptive_summary()["adaptive_terminal_failures_by_row_shape"], {"null_token": 1})
        self.assertEqual(provider.adaptive_summary()["adaptive_terminal_failures_by_line_length_bucket"], {"1-16": 1})
        for private in ("987654", "private", "NULL", "SELECT", "payload"):
            self.assertNotIn(private, out.getvalue() + err.getvalue())

    def test_provider_shape_errors_are_distinguished_without_field_leakage(self) -> None:
        import folketinget_sql_provider as canonical_provider

        expected = set(canonical_provider.PROJECTIONS["Sag"])
        cases = [
            ("not-json", "provider_json_invalid"),
            (json.dumps([{"id": 1}]), "provider_row_shape_invalid"),
            (json.dumps([{field: None for field in expected} | {"id": "private-id"}]), "provider_id_type_invalid"),
            (json.dumps([row("Sag", 1, statusid="private-value")]), "provider_relation_field_type_invalid"),
        ]
        for payload, category in cases:
            with self.subTest(category=category):
                provider = canonical_provider.SQLServerFolketingetProvider(
                    SCHEMAS, lambda query, entity, result=payload: result
                )
                with self.assertRaises(canonical_provider.SQLProviderError) as raised:
                    provider.get_by_ids("Sag", [1])
                self.assertEqual(raised.exception.category, category)
                self.assertNotIn("private", str(raised.exception))

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
        result = POC.exercise_sql_provider(
            provider,
            {entity: fixture[entity][0]["id"] for entity in ENTITIES},
            [("Sag", "statusid", 6), ("Sagstrin", "sagid", 1),
             ("SagDokument", "sagid", 1), ("SagDokument", "dokumentid", 4),
             ("Afstemning", "sagstrinid", 2)],
        )
        self.assertEqual(result["entities_tested"], 6)
        self.assertEqual(result["get_by_ids_checks"], 6)
        self.assertEqual(result["normalization_failures"], 0)
        self.assertGreaterEqual(result["relation_checks"], 5)
        self.assertEqual(len(provider.lookups), result["relation_checks"])

    def test_smoke_relation_sample_queries_are_allowlisted_and_fanout_bounded(self) -> None:
        entities = {"objects": {entity: ("dbo", "USER_TABLE") for entity in ENTITIES}}
        queries = []

        def sqlcmd(query, *args, **kwargs):
            queries.append(query)
            return "sample_value|match_count\n------------|-----------\n42|4\n"

        with mock.patch.object(POC, "sqlcmd", side_effect=sqlcmd):
            samples = POC.read_sql_provider_relation_samples("private", entities)
        self.assertEqual(len(samples), 5)
        self.assertTrue(all(item[2] == 42 for item in samples))
        self.assertEqual(len(queries), 5)
        self.assertTrue(all("SELECT TOP (1)" in query and "GROUP BY" in query for query in queries))
        self.assertTrue(all("ORDER BY COUNT_BIG(*) ASC" in query for query in queries))

        with mock.patch.object(POC, "sqlcmd", return_value="sample_value|match_count\n------------|-----------\n42|1001\n"):
            with self.assertRaises(POC.PocError) as raised:
                POC.read_sql_provider_relation_samples("private", entities)
        self.assertEqual(raised.exception.category, "sql_provider_relation_sample_too_large")

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
