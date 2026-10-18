from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "folketinget_backup_poc", ROOT / "tools" / "folketinget_backup_poc.py"
)
POC = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(POC)


class BackupPocSafetyTests(unittest.TestCase):
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

    def test_backup_must_be_full_and_supported(self) -> None:
        POC.validate_backup_header(16, 1, False)
        for args in ((18, 1, False), (16, 2, False), (16, 1, True)):
            with self.subTest(args=args), self.assertRaises(POC.PocError):
                POC.validate_backup_header(*args)

    def test_redirect_origin_rejects_credentials_and_other_hosts(self) -> None:
        self.assertEqual(POC.official_origin("https://oda.ft.dk/file"), POC.ODA_ORIGIN)
        self.assertIsNone(POC.official_origin("https://user:pass@oda.ft.dk/file"))
        self.assertNotEqual(POC.official_origin("https://example.org/file"), POC.ODA_ORIGIN)

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
        self.assertIn("runs-on: ubuntu-latest", workflow)
        self.assertIn("confirm_restore", workflow)
        self.assertIn("secrets.ODA_BACKUP_USERNAME", workflow)
        self.assertIn("secrets.ODA_BACKUP_PASSWORD", workflow)
        self.assertNotIn("upload-artifact", workflow)
        self.assertNotIn("worker", workflow.lower())
        self.assertNotIn("telegram", workflow.lower())

    def test_restore_script_has_no_production_or_telegram_route(self) -> None:
        script = (ROOT / "tools/folketinget_backup_poc.py").read_text().lower()
        for forbidden in ("workers.dev", "/internal/", "api.telegram.org", "ingestion endpoint"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, script)


if __name__ == "__main__":
    unittest.main()
