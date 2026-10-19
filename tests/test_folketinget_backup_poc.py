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
            (backup * 64, finish, "a" * 64, "2026-10-19T00:00:00Z", total),
        )
        for entity in entities:
            db.execute("INSERT INTO entity_counts VALUES (?, ?)", (entity, len(records[entity])))
            db.executemany('INSERT INTO "rows" VALUES (?, ?, ?, ?)',
                           ((entity, key, full, semantic) for key, full, semantic in records[entity]))


class BackupPocSafetyTests(unittest.TestCase):
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
        self.assertNotIn("schedule:", workflow)
        self.assertIn("inputs.mode == 'DIFF_MANIFESTS' || inputs.mode == 'EXTRACT_SNAPSHOT_CANDIDATES'", workflow)
        self.assertIn("OLD_MANIFEST_PATH:", workflow)
        self.assertIn("NEW_MANIFEST_PATH:", workflow)
        self.assertIn("INPUT_OLD_RUN_ID:", workflow)
        self.assertIn("INPUT_NEW_RUN_ID:", workflow)
        self.assertIn("if: inputs.mode != 'DIFF_MANIFESTS'", workflow)
        self.assertLess(workflow.index("name: Verify and restore official backup"),
                        workflow.index("name: Remove downloaded manifest artifacts"))
        self.assertNotIn("candidate-bundle", workflow)
        self.assertNotIn("EXTRACT_SNAPSHOT_CANDIDATES\n        uses: actions/upload-artifact", workflow)
        self.assertIn("if: success() && inputs.mode == 'CREATE_MANIFEST_BASELINE'", workflow)

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
        self.assertIn("retention-days: 2", workflow)
        self.assertIn("CREATE_MANIFEST_BASELINE", workflow)
        self.assertIn("VALIDATE_SQL_PROVIDER", workflow)
        self.assertIn("if: inputs.mode != 'DIFF_MANIFESTS'", workflow)
        self.assertIn("manifest.sqlite", workflow)
        self.assertNotIn(".bak", workflow)
        self.assertNotIn("worker", workflow.lower())
        self.assertNotIn("telegram", workflow.lower())

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
