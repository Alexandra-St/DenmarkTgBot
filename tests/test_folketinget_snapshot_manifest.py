from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock
from datetime import date, datetime, time, timezone
from decimal import Decimal


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "folketinget_snapshot_manifest", ROOT / "tools" / "folketinget_snapshot_manifest.py"
)
MANIFEST = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MANIFEST)


def schema() -> dict[str, object]:
    return {
        "schema_name": "dbo",
        "object_type": "USER_TABLE",
        "columns": [
            {"ordinal": 1, "name": "id", "type_name": "int", "max_length": 4,
             "precision": 10, "scale": 0, "nullable": 0, "key_ordinal": 1, "collation": None},
            {"ordinal": 2, "name": "title", "type_name": "nvarchar", "max_length": 400,
             "precision": 0, "scale": 0, "nullable": 1, "key_ordinal": 0, "collation": "Danish"},
            {"ordinal": 3, "name": "opdateringsdato", "type_name": "datetime2", "max_length": 8,
             "precision": 27, "scale": 7, "nullable": 1, "key_ordinal": 0, "collation": None},
            {"ordinal": 4, "name": "payload", "type_name": "varbinary", "max_length": -1,
             "precision": 0, "scale": 0, "nullable": 1, "key_ordinal": 0, "collation": None},
        ],
    }


def schemas() -> dict[str, dict[str, object]]:
    return {entity: schema() for entity in MANIFEST.REQUIRED_ENTITIES}


class FolketingetManifestTests(unittest.TestCase):
    def test_canonicalization_is_deterministic_for_supported_python_values(self) -> None:
        value = {
            "unicode": "Æøå Україна",
            "null": None,
            "decimal": Decimal("12.3400"),
            "date": date(2026, 10, 18),
            "datetime": datetime(2026, 10, 18, 12, 30, 1, 123456, tzinfo=timezone.utc),
            "time": time(12, 30, 1, 123456),
            "binary": b"\x00\xff",
            "boolean": True,
        }
        expected = MANIFEST.canonical_json_bytes(value)
        self.assertEqual(expected, MANIFEST.canonical_json_bytes(dict(reversed(list(value.items())))))
        self.assertEqual(expected, MANIFEST.canonical_json_bytes(value))
        self.assertIn("Æøå Україна".encode("utf-8"), expected)
        self.assertIn(b"$binary", expected)
        self.assertIn(b"$date", expected)
        self.assertIn(b"$datetime", expected)

    def test_semantic_hash_ignores_opdateringsdato_case_insensitively(self) -> None:
        normalized = MANIFEST.normalize_schema("Sag", schema())
        first = {"c0000": 7, "c0001": "Titel", "c0002": "2026-01-01T00:00:00.0000001", "c0003": "AP8="}
        updated = {**first, "c0002": "2026-01-02T00:00:00.0000002"}
        key1, full1, semantic1 = MANIFEST._row_hash_inputs("Sag", normalized, first)
        key2, full2, semantic2 = MANIFEST._row_hash_inputs("Sag", normalized, updated)
        self.assertEqual(key1, key2)
        self.assertNotEqual(full1, full2)
        self.assertEqual(semantic1, semantic2)
        self.assertEqual(len(full1), 32)

    def test_schema_fingerprint_is_independent_of_metadata_mapping_order(self) -> None:
        ordered = schemas()
        first = MANIFEST.schema_fingerprint(ordered)
        changed = schemas()
        changed["Sag"]["columns"] = list(reversed(changed["Sag"]["columns"]))
        self.assertEqual(first, MANIFEST.schema_fingerprint(changed))

    def _build(self, path: pathlib.Path, pages: dict[str, list[dict[str, object]]], counts: dict[str, int]):
        def fetch(entity: str, _schema_name: str, _schema: dict[str, object], offset: int, limit: int) -> str:
            return json.dumps(pages[entity][offset : offset + limit], ensure_ascii=False)

        return MANIFEST.build_manifest(
            path,
            backup_sha256="a" * 64,
            backup_finish_date="2026-10-17T02:14:06Z",
            source_sql_major=17,
            schemas=schemas(),
            expected_counts=counts,
            fetch_page=fetch,
            batch_size=1,
            generated_at="2026-10-18T00:00:00Z",
        )

    def test_manifest_streams_rows_and_stores_only_key_and_hashes(self) -> None:
        pages = {
            entity: [{"c0000": index, "c0001": "PRIVATE ROW CONTENT", "c0002": None, "c0003": None}]
            for index, entity in enumerate(MANIFEST.REQUIRED_ENTITIES, start=1)
        }
        counts = {entity: 1 for entity in MANIFEST.REQUIRED_ENTITIES}
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "manifest.sqlite"
            result = self._build(path, pages, counts)
            self.assertEqual(result["total_rows"], 6)
            self.assertEqual(result["validation"], "PASS")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(path) as db:
                columns = [row[1] for row in db.execute('PRAGMA table_info("rows")')]
                self.assertEqual(columns, ["entity", "canonical_key", "full_hash", "semantic_hash"])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM "rows"').fetchone()[0], 6)
                self.assertEqual(db.execute('SELECT length(full_hash), length(semantic_hash) FROM "rows" LIMIT 1').fetchone(), (32, 32))
            self.assertNotIn(b"PRIVATE ROW CONTENT", path.read_bytes())

    def test_duplicate_canonical_keys_are_rejected_and_partial_manifest_removed(self) -> None:
        pages = {entity: [] for entity in MANIFEST.REQUIRED_ENTITIES}
        pages["Sag"] = [
            {"c0000": 1, "c0001": "one", "c0002": None, "c0003": None},
            {"c0000": 1, "c0001": "two", "c0002": None, "c0003": None},
        ]
        counts = {entity: 0 for entity in MANIFEST.REQUIRED_ENTITIES}
        counts["Sag"] = 2
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "manifest.sqlite"
            with self.assertRaises(MANIFEST.ManifestError) as raised:
                self._build(path, pages, counts)
            self.assertEqual(raised.exception.category, "duplicate_canonical_key")
            self.assertFalse(path.exists())

    def test_row_count_mismatch_fails_closed(self) -> None:
        pages = {entity: [] for entity in MANIFEST.REQUIRED_ENTITIES}
        pages["Sag"] = [{"c0000": 1, "c0001": None, "c0002": None, "c0003": None}]
        counts = {entity: 0 for entity in MANIFEST.REQUIRED_ENTITIES}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MANIFEST.ManifestError) as raised:
                self._build(pathlib.Path(directory) / "manifest.sqlite", pages, counts)
        self.assertEqual(raised.exception.category, "manifest_row_count_mismatch")

    def test_unsupported_sql_type_fails_closed(self) -> None:
        unsupported = schema()
        unsupported["columns"][1]["type_name"] = "geometry"
        inputs = schemas()
        inputs["Sag"] = unsupported
        with self.assertRaises(MANIFEST.ManifestError) as raised:
            MANIFEST.schema_fingerprint(inputs)
        self.assertEqual(raised.exception.category, "unsupported_sql_type")

    def test_workflow_upload_is_manual_scoped_short_lived_and_manifest_only(self) -> None:
        workflow = (ROOT / ".github/workflows/folketinget-backup-poc.yml").read_text()
        self.assertIn("CREATE_MANIFEST_BASELINE", workflow)
        self.assertIn("uses: actions/upload-artifact@v7", workflow)
        self.assertIn("retention-days: 2", workflow)
        self.assertIn("manifest.sqlite", workflow)
        self.assertIn("if: success() && inputs.mode == 'CREATE_MANIFEST_BASELINE'", workflow)
        self.assertIn("if: always() && inputs.mode == 'CREATE_MANIFEST_BASELINE'", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn(".bak", workflow)
        self.assertNotIn(".mdf", workflow)
        self.assertNotIn(".ldf", workflow)

    def test_backup_mode_is_explicit_and_rejects_unknown_values(self) -> None:
        poc_spec = importlib.util.spec_from_file_location(
            "folketinget_backup_poc_manifest_test", ROOT / "tools" / "folketinget_backup_poc.py"
        )
        poc = importlib.util.module_from_spec(poc_spec)
        assert poc_spec.loader is not None
        poc_spec.loader.exec_module(poc)
        self.assertEqual(poc.validate_mode("CREATE_MANIFEST_BASELINE"), "CREATE_MANIFEST_BASELINE")
        self.assertEqual(poc.validate_mode("RESTORE_VALIDATE"), "RESTORE_VALIDATE")
        with self.assertRaises(poc.PocError):
            poc.validate_mode("PRODUCTION_INGEST")

    def test_manifest_sql_orders_by_pk_and_limits_page_size(self) -> None:
        poc_spec = importlib.util.spec_from_file_location(
            "folketinget_backup_poc_manifest_sql_test", ROOT / "tools" / "folketinget_backup_poc.py"
        )
        poc = importlib.util.module_from_spec(poc_spec)
        assert poc_spec.loader is not None
        poc_spec.loader.exec_module(poc)
        schema_info = MANIFEST.normalize_schema("Sag", schema())
        page = '[{"c0000":1,"c0001":"Æ","c0002":null,"c0003":null}]'
        with mock.patch.object(poc, "run_manifest_page_sql", return_value=page + "\n") as run:
            payload = poc.manifest_page_json("private", "Sag", "dbo", schema_info, 5000, 5000)
        self.assertEqual(MANIFEST._decode_json_page(payload), [{"c0000": 1, "c0001": "Æ", "c0002": None, "c0003": None}])
        argv = run.call_args.args[0]
        self.assertIn("-y", argv)
        self.assertIn("0", argv)
        self.assertNotIn("-W", argv)
        self.assertIn("-r", argv)
        query = argv[-1]
        self.assertIn("ORDER BY [id] OFFSET 5000 ROWS FETCH NEXT 5000 ROWS ONLY", query)
        self.assertIn("FOR JSON PATH, INCLUDE_NULL_VALUES", query)
        self.assertNotIn("private", query)

    def test_manifest_page_converts_legacy_text_types_before_for_json(self) -> None:
        poc_spec = importlib.util.spec_from_file_location(
            "folketinget_backup_poc_legacy_types_test", ROOT / "tools" / "folketinget_backup_poc.py"
        )
        poc = importlib.util.module_from_spec(poc_spec)
        assert poc_spec.loader is not None
        poc_spec.loader.exec_module(poc)
        source_schema = schema()
        source_schema["columns"].extend([
            {"ordinal": 5, "name": "legacy_text", "type_name": "text", "max_length": 16,
             "precision": 0, "scale": 0, "nullable": 1, "key_ordinal": 0, "collation": "Danish"},
            {"ordinal": 6, "name": "legacy_ntext", "type_name": "ntext", "max_length": 16,
             "precision": 0, "scale": 0, "nullable": 1, "key_ordinal": 0, "collation": "Danish"},
        ])
        schema_info = MANIFEST.normalize_schema("Sag", source_schema)
        page = '[{"c0000":1,"c0001":null,"c0002":null,"c0003":null,"c0004":"old","c0005":"æ"}]'
        with mock.patch.object(poc, "run_manifest_page_sql", return_value=page) as run:
            payload = poc.manifest_page_json("private", "Sag", "dbo", schema_info, 0, 10)
        query = run.call_args.args[0][-1]
        self.assertIn("CONVERT(varchar(max), [legacy_text]) AS [c0004]", query)
        self.assertIn("CONVERT(nvarchar(max), [legacy_ntext]) AS [c0005]", query)
        self.assertEqual(MANIFEST._decode_json_page(payload)[0]["c0005"], "æ")

    def test_sql_failure_logs_only_safe_entity_offset_code_and_category(self) -> None:
        poc_spec = importlib.util.spec_from_file_location(
            "folketinget_backup_poc_sql_diagnostics_test", ROOT / "tools" / "folketinget_backup_poc.py"
        )
        poc = importlib.util.module_from_spec(poc_spec)
        assert poc_spec.loader is not None
        poc_spec.loader.exec_module(poc)
        stderr = StringIO()
        failure = mock.Mock(returncode=1, stdout='[{"row":"PRIVATE_STDOUT"}]', stderr="Msg 402, Level 16: PRIVATE_SQL_DETAIL PRIVATE_ID")
        with mock.patch.object(poc.subprocess, "run", return_value=failure):
            with redirect_stderr(stderr), self.assertRaises(poc.PocError) as raised:
                poc.run_manifest_page_sql(["sqlcmd", "PRIVATE_QUERY"], "PRIVATE_PASSWORD", entity="Sag", offset=5000, timeout=10)
        self.assertEqual(raised.exception.category, "manifest_page_sql_msg_402")
        rendered = stderr.getvalue()
        self.assertIn("entity=Sag offset=5000 return_code=1 category=manifest_page_sql_msg_402", rendered)
        for secret in ("PRIVATE_SQL_DETAIL", "PRIVATE_ID", "PRIVATE_STDOUT", "PRIVATE_QUERY", "PRIVATE_PASSWORD"):
            self.assertNotIn(secret, rendered)

    def test_sql_message_number_is_extracted_from_stdout_fallback(self) -> None:
        poc_spec = importlib.util.spec_from_file_location(
            "folketinget_backup_poc_sql_stdout_test", ROOT / "tools" / "folketinget_backup_poc.py"
        )
        poc = importlib.util.module_from_spec(poc_spec)
        assert poc_spec.loader is not None
        poc_spec.loader.exec_module(poc)
        failure = mock.Mock(returncode=1, stdout="Msg 207, Level 16: PRIVATE_VALUE", stderr="")
        logged = StringIO()
        with mock.patch.object(poc.subprocess, "run", return_value=failure), redirect_stderr(logged):
            with self.assertRaises(poc.PocError) as raised:
                poc.run_manifest_page_sql(["sqlcmd"], "PRIVATE_PASSWORD", entity="Dokument", offset=25, timeout=10)
        self.assertEqual(raised.exception.category, "manifest_page_sql_msg_207")
        self.assertIn("entity=Dokument offset=25 return_code=1 category=manifest_page_sql_msg_207", logged.getvalue())
        self.assertNotIn("PRIVATE_VALUE", logged.getvalue())
        self.assertNotIn("PRIVATE_PASSWORD", logged.getvalue())

    def test_sql_failure_without_message_number_uses_generic_safe_category(self) -> None:
        poc_spec = importlib.util.spec_from_file_location(
            "folketinget_backup_poc_sql_generic_test", ROOT / "tools" / "folketinget_backup_poc.py"
        )
        poc = importlib.util.module_from_spec(poc_spec)
        assert poc_spec.loader is not None
        poc_spec.loader.exec_module(poc)
        failure = mock.Mock(returncode=2, stdout="", stderr="connection details that must stay private")
        with mock.patch.object(poc.subprocess, "run", return_value=failure), redirect_stderr(StringIO()) as output:
            with self.assertRaises(poc.PocError) as raised:
                poc.run_manifest_page_sql(["sqlcmd"], "private", entity="Sag", offset=0, timeout=10)
        self.assertEqual(raised.exception.category, "manifest_page_sqlcmd_failed")
        self.assertNotIn("connection details", output.getvalue())

    def test_manifest_entity_progress_uses_only_allowed_entity(self) -> None:
        poc_spec = importlib.util.spec_from_file_location(
            "folketinget_backup_poc_progress_test", ROOT / "tools" / "folketinget_backup_poc.py"
        )
        poc = importlib.util.module_from_spec(poc_spec)
        assert poc_spec.loader is not None
        poc_spec.loader.exec_module(poc)
        output = StringIO()
        with redirect_stdout(output):
            poc.report_manifest_entity("Sagstrin")
        self.assertEqual(output.getvalue(), "progress=manifest_entity entity=Sagstrin\n")
        with self.assertRaises(poc.PocError):
            poc.report_manifest_entity("private")

    def test_compatibility_level_parsing_and_offset_fetch_gate_fail_closed(self) -> None:
        poc_spec = importlib.util.spec_from_file_location(
            "folketinget_backup_poc_compatibility_test", ROOT / "tools" / "folketinget_backup_poc.py"
        )
        poc = importlib.util.module_from_spec(poc_spec)
        assert poc_spec.loader is not None
        poc_spec.loader.exec_module(poc)
        self.assertEqual(poc.parse_database_compatibility_level("160\n(1 rows affected)\n"), 160)
        poc.validate_manifest_compatibility_level(160)
        with self.assertRaises(poc.PocError) as raised:
            poc.parse_database_compatibility_level("not numeric")
        self.assertEqual(raised.exception.category, "manifest_database_compatibility_invalid")
        with self.assertRaises(poc.PocError) as raised:
            poc.parse_database_compatibility_level("100\n110\n")
        self.assertEqual(raised.exception.category, "manifest_database_compatibility_invalid")
        with self.assertRaises(poc.PocError) as raised:
            poc.validate_manifest_compatibility_level(100)
        self.assertEqual(raised.exception.category, "manifest_database_compatibility_unsupported")
        output = StringIO()
        with redirect_stdout(output):
            poc.report_manifest_compatibility(160)
        self.assertEqual(output.getvalue(), "progress=manifest_database_compatibility level=160\n")

    def test_manifest_progress_and_summary_do_not_print_row_values(self) -> None:
        source = (ROOT / "tools/folketinget_backup_poc.py").read_text()
        self.assertIn("progress=manifest_generation", source)
        self.assertIn("progress=manifest_schema_discovery", source)
        self.assertIn('f"- Manifest SHA-256: {manifest[\'sha256\']}"', source)
        self.assertNotIn("print(canonical_key", source)
        self.assertNotIn("print(row", source)


if __name__ == "__main__":
    unittest.main()
