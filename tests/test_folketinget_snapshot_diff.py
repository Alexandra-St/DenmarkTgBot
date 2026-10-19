from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "folketinget_snapshot_diff", ROOT / "tools" / "folketinget_snapshot_diff.py"
)
DIFF = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(DIFF)


def create_manifest(path: pathlib.Path, records: dict[str, list[tuple[str, bytes, bytes]]], *, fingerprint: str = "a" * 64) -> None:
    total = sum(len(rows) for rows in records.values())
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
            ("1" * 64, "2026-10-18T02:00:00Z", fingerprint, "2026-10-18T03:00:00Z", total),
        )
        for entity in DIFF.manifest.REQUIRED_ENTITIES:
            rows = records.get(entity, [])
            db.execute("INSERT INTO entity_counts VALUES (?, ?)", (entity, len(rows)))
            db.executemany('INSERT INTO "rows" VALUES (?, ?, ?, ?)', ((entity, key, full, semantic) for key, full, semantic in rows))


class FolketingetSnapshotDiffTests(unittest.TestCase):
    def test_all_categories_and_aggregate_report_have_no_row_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = pathlib.Path(temp)
            old = directory / "old.sqlite"
            new = directory / "new.sqlite"
            create_manifest(old, {"Sag": [
                ("PRIVATE_KEY_UNCHANGED", b"a" * 32, b"u" * 32),
                ("PRIVATE_KEY_CHANGED", b"b" * 32, b"c" * 32),
                ("PRIVATE_KEY_TECHNICAL", b"d" * 32, b"e" * 32),
                ("PRIVATE_KEY_REMOVED", b"f" * 32, b"g" * 32),
            ]})
            create_manifest(new, {"Sag": [
                ("PRIVATE_KEY_UNCHANGED", b"a" * 32, b"u" * 32),
                ("PRIVATE_KEY_CHANGED", b"h" * 32, b"i" * 32),
                ("PRIVATE_KEY_TECHNICAL", b"j" * 32, b"e" * 32),
                ("PRIVATE_KEY_ADDED", b"k" * 32, b"l" * 32),
            ]})
            report = DIFF.compare_manifests(old, new)
            self.assertEqual(report["validation"], "PASS")
            self.assertEqual(report["entities"]["Sag"], {
                "added": 1, "removed": 1, "changed": 1, "technical_only": 1, "unchanged": 1,
            })
            self.assertEqual(report["totals"]["added"], 1)
            self.assertEqual(sum(report["totals"].values()), 5)
            output = json.dumps(report)
            for key in ("PRIVATE_KEY_UNCHANGED", "PRIVATE_KEY_CHANGED", "PRIVATE_KEY_TECHNICAL", "PRIVATE_KEY_REMOVED", "PRIVATE_KEY_ADDED"):
                self.assertNotIn(key, output)
            self.assertNotIn((b"a" * 32).hex(), output)
            self.assertEqual(report["old"]["backup_sha256"], "1" * 64)

    def test_schema_mismatch_fails_with_dedicated_category(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            old, new = pathlib.Path(temp) / "old.sqlite", pathlib.Path(temp) / "new.sqlite"
            create_manifest(old, {}, fingerprint="a" * 64)
            create_manifest(new, {}, fingerprint="b" * 64)
            with self.assertRaises(DIFF.DiffError) as raised:
                DIFF.compare_manifests(old, new)
            self.assertEqual(raised.exception.category, "manifest_schema_mismatch")

    def test_invalid_sqlite_fails_integrity_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bad, good = pathlib.Path(temp) / "bad.sqlite", pathlib.Path(temp) / "good.sqlite"
            bad.write_bytes(b"not a database")
            create_manifest(good, {})
            with self.assertRaises(DIFF.DiffError) as raised:
                DIFF.compare_manifests(bad, good)
            self.assertEqual(raised.exception.category, "manifest_integrity_check_failed")

    def test_wrong_manifest_structure_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bad, good = pathlib.Path(temp) / "bad.sqlite", pathlib.Path(temp) / "good.sqlite"
            with sqlite3.connect(bad) as db:
                db.execute("CREATE TABLE unexpected (value TEXT)")
            create_manifest(good, {})
            with self.assertRaises(DIFF.DiffError) as raised:
                DIFF.compare_manifests(bad, good)
            self.assertEqual(raised.exception.category, "manifest_structure_invalid")

    def test_wrong_required_entities_and_counts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bad, good = pathlib.Path(temp) / "bad.sqlite", pathlib.Path(temp) / "good.sqlite"
            create_manifest(bad, {})
            create_manifest(good, {})
            with sqlite3.connect(bad) as db:
                db.execute("DELETE FROM entity_counts WHERE entity='Sag'")
            with self.assertRaises(DIFF.DiffError) as raised:
                DIFF.compare_manifests(bad, good)
            self.assertEqual(raised.exception.category, "manifest_required_entities_invalid")

    def test_artifact_directory_must_contain_only_expected_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            old_dir, new_dir = root / "old", root / "new"
            old_dir.mkdir(); new_dir.mkdir()
            create_manifest(old_dir / "manifest.sqlite", {})
            create_manifest(new_dir / "manifest.sqlite", {})
            self.assertEqual(DIFF.compare_manifests(old_dir, new_dir)["validation"], "PASS")
            (new_dir / "unexpected.txt").write_text("extra")
            with self.assertRaises(DIFF.DiffError) as raised:
                DIFF.compare_manifests(old_dir, new_dir)
            self.assertEqual(raised.exception.category, "manifest_artifact_invalid")

    def test_workflow_diff_mode_is_manual_and_checks_two_successful_artifacts(self) -> None:
        workflow = (ROOT / ".github/workflows/folketinget-backup-poc.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("- DIFF_MANIFESTS", workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("actions/download-artifact@v4", workflow)
        self.assertIn("OLD_RUN_ID", workflow)
        self.assertIn("NEW_RUN_ID", workflow)
        self.assertIn("conclusion == \"success\"", workflow)
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("manifest.sqlite", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("npm run deploy", workflow)

    def test_large_diff_is_aggregated_by_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            old, new = pathlib.Path(temp) / "old.sqlite", pathlib.Path(temp) / "new.sqlite"
            rows = [(f"private-{i}", i.to_bytes(32, "big"), i.to_bytes(32, "big")) for i in range(12000)]
            create_manifest(old, {"Sag": rows})
            create_manifest(new, {"Sag": rows[:-1] + [("private-11999", b"z" * 32, b"z" * 32)]})
            report = DIFF.compare_manifests(old, new)
            self.assertEqual(report["entities"]["Sag"]["changed"], 1)
            self.assertEqual(report["totals"]["unchanged"], 11999)
        source = (ROOT / "tools" / "folketinget_snapshot_diff.py").read_text()
        self.assertIn("GROUP BY entity, category", source)
        self.assertNotIn("SELECT * FROM main.\"rows\"", source)

    def test_cli_emits_only_aggregate_json_and_safe_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            old, new = root / "old.sqlite", root / "new.sqlite"
            create_manifest(old, {"Sag": [("sensitive-key", b"a" * 32, b"b" * 32)]})
            create_manifest(new, {"Sag": [("sensitive-key", b"c" * 32, b"d" * 32)]})
            output = StringIO()
            with redirect_stdout(output):
                code = DIFF.main(["--old", str(old), "--new", str(new)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["totals"]["changed"], 1)
            self.assertNotIn("sensitive-key", output.getvalue())
            error_output = StringIO()
            with redirect_stderr(error_output):
                code = DIFF.main(["--old", str(old), "--new", str(root / "missing.sqlite")])
            self.assertEqual(code, 2)
            self.assertIn("category=manifest_artifact_invalid", error_output.getvalue())
            self.assertNotIn("sensitive-key", error_output.getvalue())


if __name__ == "__main__":
    unittest.main()
