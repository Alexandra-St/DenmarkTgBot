#!/usr/bin/env python3
"""Manual-only, read-only restore validation for the official ODA backup."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ODA_URL = "https://oda.ft.dk/odapublish/oda.bak"
ODA_ORIGIN = ("https", "oda.ft.dk", 443)
REQUIRED_ENTITIES = ("Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus")
MAX_BACKUP_BYTES = 5 * 1024**3
GIB = 1024**3
SQL_MAJOR = 17  # SQL Server 2025
SQLCMD = "/opt/mssql-tools18/bin/sqlcmd"
DATABASE = "DenmarkBotFolketingetBackupPoc"


class PocError(Exception):
    """Safe error category; messages must not contain external values."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def validate_confirmation(value: str) -> None:
    if value != "true":
        raise PocError("manual_confirmation_required")


def validate_credentials(username: str, password: str) -> None:
    if not username or not password:
        raise PocError("backup_credentials_missing")


def official_origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError:
        return None
    if parsed.username or parsed.password:
        return None
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def require_x64_ubuntu24(machine: str, release: str) -> None:
    if machine.lower() not in {"x86_64", "amd64"}:
        raise PocError("runner_architecture_unsupported")
    if release != "24.04":
        raise PocError("runner_os_unsupported")


def check_free_space(free_bytes: int, required_bytes: int) -> None:
    if free_bytes < required_bytes:
        raise PocError("insufficient_disk_space")


def download_space_required(content_length: int) -> int:
    if content_length <= 0 or content_length > MAX_BACKUP_BYTES:
        raise PocError("backup_size_missing_or_over_limit")
    return content_length + 2 * GIB


def restore_space_required(file_sizes: list[int]) -> int:
    if not file_sizes or any(size <= 0 for size in file_sizes):
        raise PocError("backup_filelist_invalid")
    return sum(file_sizes) + max(GIB, sum(file_sizes) // 5)


def validate_backup_header(major: int, backup_type: int, damaged: bool) -> None:
    if major < 9 or major > SQL_MAJOR:
        raise PocError("backup_sql_version_unsupported")
    if backup_type != 1:
        raise PocError("backup_is_not_full_database")
    if damaged:
        raise PocError("backup_marked_damaged")


def quote_identifier(value: str) -> str:
    if not value or "\x00" in value:
        raise PocError("backup_metadata_invalid")
    return "[" + value.replace("]", "]]" ) + "]"


def quote_literal(value: str) -> str:
    if "\x00" in value:
        raise PocError("backup_metadata_invalid")
    return "N'" + value.replace("'", "''") + "'"


def parse_sqlcmd_table(output: str) -> tuple[list[str], list[list[str]]]:
    """Parse sqlcmd -s '|' output without emitting the source text."""
    lines = [line.strip("\r") for line in output.splitlines() if line.strip()]
    header_index = next(
        (i for i, line in enumerate(lines) if "|" in line and not set(line) <= set("-|+ ")),
        None,
    )
    if header_index is None:
        raise PocError("sql_metadata_unparseable")
    headers = [part.strip() for part in lines[header_index].split("|")]
    rows: list[list[str]] = []
    for line in lines[header_index + 1 :]:
        if line.startswith("(") and "rows affected" in line.lower():
            break
        if "|" not in line or set(line) <= set("-|+ "):
            continue
        row = [part.strip() for part in line.split("|")]
        if len(row) != len(headers):
            raise PocError("sql_metadata_unparseable")
        rows.append(row)
    return headers, rows


def field_index(headers: list[str], name: str) -> int:
    normalized = [re.sub(r"[^a-z0-9]", "", item.lower()) for item in headers]
    wanted = re.sub(r"[^a-z0-9]", "", name.lower())
    try:
        return normalized.index(wanted)
    except ValueError as exc:
        raise PocError("sql_metadata_field_missing") from exc


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if official_origin(newurl) != ODA_ORIGIN:
            raise PocError("backup_redirect_blocked")
        return urllib.request.Request(newurl, headers=request.headers, method=request.get_method())


def make_oda_opener(username: str, password: str) -> urllib.request.OpenerDirector:
    manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    manager.add_password(None, "https://oda.ft.dk/", username, password)
    return urllib.request.build_opener(SameOriginRedirectHandler(), urllib.request.HTTPBasicAuthHandler(manager))


def request_content_length(opener: urllib.request.OpenerDirector) -> int:
    if official_origin(ODA_URL) != ODA_ORIGIN:
        raise PocError("backup_url_not_allowlisted")
    request = urllib.request.Request(ODA_URL, method="HEAD", headers={"User-Agent": "DenmarkBot backup PoC"})
    try:
        with opener.open(request, timeout=20) as response:
            if response.status != 200 or official_origin(response.geturl()) != ODA_ORIGIN:
                raise PocError("backup_head_failed")
            length = response.headers.get("Content-Length")
            if length is None or not length.isdigit():
                raise PocError("backup_length_unavailable")
            return int(length)
    except PocError:
        raise
    except urllib.error.HTTPError as exc:
        raise PocError(f"backup_head_http_{exc.code}") from None
    except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException):
        raise PocError("backup_head_network_error") from None


def download_backup(opener: urllib.request.OpenerDirector, target: Path, expected_bytes: int) -> dict[str, object]:
    request = urllib.request.Request(ODA_URL, headers={"User-Agent": "DenmarkBot backup PoC"})
    started = time.monotonic()
    digest = hashlib.sha256()
    written = 0
    deadline = started + 20 * 60
    try:
        with opener.open(request, timeout=30) as response, target.open("wb") as output:
            if response.status != 200 or official_origin(response.geturl()) != ODA_ORIGIN:
                raise PocError("backup_download_response_invalid")
            response_length = response.headers.get("Content-Length")
            if response_length is not None and response_length.isdigit() and int(response_length) != expected_bytes:
                raise PocError("backup_size_changed_during_download")
            while True:
                if time.monotonic() >= deadline:
                    raise PocError("backup_download_deadline")
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > expected_bytes or written > MAX_BACKUP_BYTES:
                    raise PocError("backup_size_exceeded")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if written != expected_bytes:
            raise PocError("backup_download_incomplete")
        target.chmod(0o640)
        run_checked(["sudo", "chgrp", "mssql", str(target)], "backup_permissions_failed")
        return {
            "file_size_bytes": written,
            "sha256": digest.hexdigest(),
            "download_seconds": round(time.monotonic() - started, 2),
        }
    except PocError:
        target.unlink(missing_ok=True)
        raise
    except urllib.error.HTTPError as exc:
        target.unlink(missing_ok=True)
        raise PocError(f"backup_download_http_{exc.code}") from None
    except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException):
        target.unlink(missing_ok=True)
        raise PocError("backup_download_network_error") from None


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def run_checked(argv: list[str], category: str, *, env: dict[str, str] | None = None, timeout: int = 1800, input_bytes: bytes | None = None) -> str:
    try:
        result = subprocess.run(argv, input=input_bytes, text=input_bytes is None, capture_output=True, check=False, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise PocError(category) from None
    if result.returncode != 0:
        raise PocError(category)
    return result.stdout if isinstance(result.stdout, str) else result.stdout.decode(errors="replace")


def sql_env(password: str) -> dict[str, str]:
    blocked = {"ODA_BACKUP_USERNAME", "ODA_BACKUP_PASSWORD", "MSSQL_SA_PASSWORD"}
    result = {key: value for key, value in os.environ.items() if key not in blocked}
    result["SQLCMDPASSWORD"] = password
    return result


def sqlcmd(query: str, password: str, *, timeout: int = 900, database: str = "master") -> str:
    return run_checked(
        [SQLCMD, "-S", "localhost", "-U", "sa", "-C", "-b", "-W", "-s", "|", "-w", "65535", "-d", database, "-Q", query],
        "sql_server_command_failed", env=sql_env(password), timeout=timeout,
    )


def simple_value(output: str) -> str:
    values = [line.strip() for line in output.splitlines() if line.strip() and not line.strip().startswith("(")]
    if not values:
        raise PocError("sql_result_missing")
    return values[-1]


def fetch_microsoft_file(url: str, failure_category: str) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "packages.microsoft.com" or parsed.username or parsed.password:
        raise PocError("microsoft_repo_setup_failed")
    request = urllib.request.Request(url, headers={"User-Agent": "DenmarkBot backup PoC"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != "packages.microsoft.com":
                raise PocError(failure_category)
            content = response.read(64 * 1024 + 1)
            if not content or len(content) > 64 * 1024:
                raise PocError(failure_category)
            return content
    except PocError:
        raise
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
        raise PocError(failure_category) from None


def validate_repository_list(content: bytes, repository: str) -> bytes:
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise PocError("microsoft_repo_setup_failed") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    expected = f"https://packages.microsoft.com/ubuntu/24.04/{repository}"
    if not lines or any(not line.startswith("deb ") or expected not in line.split() for line in lines):
        raise PocError("microsoft_repo_setup_failed")
    return content


def run_sql_server_setup(password: str) -> None:
    # Follow Microsoft's Ubuntu 24.04 SQL Server 2025 and tools repository setup.
    key = fetch_microsoft_file(
        "https://packages.microsoft.com/keys/microsoft.asc", "microsoft_key_download_failed"
    )
    if not key.startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----"):
        raise PocError("microsoft_key_download_failed")
    server_list = fetch_microsoft_file(
        "https://packages.microsoft.com/config/ubuntu/24.04/mssql-server-2025.list",
        "microsoft_repo_setup_failed",
    )
    tools_list = fetch_microsoft_file(
        "https://packages.microsoft.com/config/ubuntu/24.04/prod.list",
        "microsoft_repo_setup_failed",
    )
    validate_repository_list(server_list, "mssql-server-2025")
    validate_repository_list(tools_list, "prod")
    run_checked(
        ["sudo", "tee", "/etc/apt/trusted.gpg.d/microsoft.asc"],
        "microsoft_repo_setup_failed", input_bytes=key,
    )
    run_checked(
        ["sudo", "tee", "/etc/apt/sources.list.d/mssql-server-2025.list"],
        "microsoft_repo_setup_failed", input_bytes=server_list,
    )
    run_checked(
        ["sudo", "tee", "/etc/apt/sources.list.d/mssql-release.list"],
        "microsoft_repo_setup_failed", input_bytes=tools_list,
    )
    run_checked(["sudo", "apt-get", "update", "-qq"], "apt_update_failed", timeout=600)
    install_env = {**sql_env(""), "ACCEPT_EULA": "Y"}
    run_checked(["sudo", "env", "ACCEPT_EULA=Y", "apt-get", "install", "-y", "-qq", "mssql-server", "mssql-tools18"], "sql_server_package_install_failed", env=install_env, timeout=1200)
    setup_env = {**sql_env(""), "ACCEPT_EULA": "Y", "MSSQL_PID": "Express", "MSSQL_SA_PASSWORD": password}
    run_checked(
        ["sudo", "--preserve-env=ACCEPT_EULA,MSSQL_PID,MSSQL_SA_PASSWORD", "/opt/mssql/bin/mssql-conf", "-n", "setup"],
        "sql_server_express_setup_failed", env=setup_env, timeout=600,
    )
    run_checked(["sudo", "systemctl", "start", "mssql-server"], "sql_server_start_failed", timeout=120)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            edition = simple_value(sqlcmd("SELECT CAST(SERVERPROPERTY('Edition') AS nvarchar(128));", password, timeout=15))
            major_text = simple_value(sqlcmd("SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS nvarchar(20));", password, timeout=15))
            if "Express" not in edition or major_text != str(SQL_MAJOR):
                raise PocError("sql_server_express_version_unverified")
            return
        except PocError as exc:
            if exc.category == "sql_server_express_version_unverified":
                raise
            time.sleep(3)
    raise PocError("sql_server_start_failed")


def _normalized_headers(output: str) -> tuple[list[str], list[list[str]]]:
    headers, rows = parse_sqlcmd_table(output)
    return headers, rows


def header_metadata(output: str) -> dict[str, object]:
    headers, rows = _normalized_headers(output)
    if len(rows) != 1:
        raise PocError("backup_header_count_invalid")
    row = rows[0]
    major = int(row[field_index(headers, "SoftwareVersionMajor")])
    backup_type = int(row[field_index(headers, "BackupType")])
    damaged_value = row[field_index(headers, "IsDamaged")].lower()
    validate_backup_header(major, backup_type, damaged_value in {"1", "true"})
    return {
        "source_sql_major": major,
        "backup_type": backup_type,
        "compressed": row[field_index(headers, "IsCompressed")],
        "backup_finish_date": row[field_index(headers, "BackupFinishDate")],
    }


def filelist_metadata(output: str) -> list[dict[str, object]]:
    headers, rows = _normalized_headers(output)
    entries: list[dict[str, object]] = []
    for row in rows:
        file_type = row[field_index(headers, "Type")]
        if file_type not in {"D", "L"}:
            raise PocError("backup_file_type_unsupported")
        size = int(row[field_index(headers, "Size")])
        logical_name = row[field_index(headers, "LogicalName")]
        if not logical_name or size <= 0:
            raise PocError("backup_filelist_invalid")
        entries.append({"logical_name": logical_name, "type": file_type, "size": size})
    if not entries or not any(item["type"] == "D" for item in entries):
        raise PocError("backup_filelist_invalid")
    if sum(int(item["size"]) for item in entries if item["type"] == "D") > 50 * GIB:
        raise PocError("express_database_size_limit_exceeded")
    return entries


def build_restore_query(backup_path: str, data_dir: str, entries: list[dict[str, object]]) -> str:
    clauses: list[str] = []
    data_index = 0
    log_index = 0
    for entry in entries:
        if entry["type"] == "D":
            suffix = ".mdf" if data_index == 0 else ".ndf"
            data_index += 1
        elif entry["type"] == "L":
            suffix = ".ldf"
            log_index += 1
        else:
            raise PocError("backup_file_type_unsupported")
        name = f"file-{data_index if entry['type'] == 'D' else log_index}{suffix}"
        destination = str(Path(data_dir) / name)
        clauses.append(f"MOVE {quote_literal(str(entry['logical_name']))} TO {quote_literal(destination)}")
    parsed_backup_path = urllib.parse.urlsplit(backup_path)
    if parsed_backup_path.scheme or parsed_backup_path.netloc or not backup_path.startswith("/"):
        raise PocError("backup_path_invalid")
    return (
        f"RESTORE DATABASE {quote_identifier(DATABASE)} FROM DISK = {quote_literal(backup_path)} "
        f"WITH FILE = 1, {', '.join(clauses)}, RECOVERY, STATS = 10;"
    )


def table_objects(password: str) -> dict[str, tuple[str, str]]:
    names = ",".join(quote_literal(name) for name in REQUIRED_ENTITIES)
    query = (
        "SELECT s.name, o.name, o.type_desc FROM sys.objects AS o "
        "JOIN sys.schemas AS s ON s.schema_id=o.schema_id "
        f"WHERE o.type IN ('U','V') AND o.name IN ({names}) ORDER BY o.name,s.name;"
    )
    output = sqlcmd(query, password, database=DATABASE)
    found: dict[str, tuple[str, str]] = {}
    for line in output.splitlines():
        fields = [value.strip() for value in line.split("|")]
        if len(fields) == 3 and fields[1] in REQUIRED_ENTITIES:
            if fields[1] in found:
                raise PocError("entity_mapping_ambiguous")
            found[fields[1]] = (fields[0], fields[2])
    return found


def validate_entities(password: str) -> dict[str, Any]:
    objects = table_objects(password)
    counts: dict[str, int] = {}
    pk: dict[str, list[str]] = {name: [] for name in REQUIRED_ENTITIES}
    fk: list[tuple[str, str, str, str]] = []
    for name, (schema, object_type) in objects.items():
        full_name = f"{quote_identifier(schema)}.{quote_identifier(name)}"
        counts[name] = int(simple_value(sqlcmd(f"SELECT COUNT_BIG(*) FROM {full_name};", password, database=DATABASE, timeout=600)))
        if object_type == "USER_TABLE":
            pk_query = (
                "SELECT c.name FROM sys.indexes AS i "
                "JOIN sys.index_columns AS ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id "
                "JOIN sys.columns AS c ON c.object_id=ic.object_id AND c.column_id=ic.column_id "
                f"WHERE i.is_primary_key=1 AND i.object_id=OBJECT_ID({quote_literal(schema + '.' + name)}) "
                "ORDER BY ic.key_ordinal;"
            )
            pk_headers, pk_rows = _normalized_headers(sqlcmd(pk_query, password, database=DATABASE))
            pk[name] = [row[field_index(pk_headers, "name")] for row in pk_rows]
    fk_query = (
        "SELECT pt.name AS parent_table, pc.name AS parent_column, "
        "rt.name AS referenced_table, rc.name AS referenced_column "
        "FROM sys.foreign_key_columns AS fkc "
        "JOIN sys.foreign_keys AS fk ON fk.object_id=fkc.constraint_object_id "
        "JOIN sys.tables AS pt ON pt.object_id=fkc.parent_object_id "
        "JOIN sys.columns AS pc ON pc.object_id=fkc.parent_object_id AND pc.column_id=fkc.parent_column_id "
        "JOIN sys.tables AS rt ON rt.object_id=fkc.referenced_object_id "
        "JOIN sys.columns AS rc ON rc.object_id=fkc.referenced_object_id AND rc.column_id=fkc.referenced_column_id "
        f"WHERE pt.name IN ({','.join(quote_literal(name) for name in REQUIRED_ENTITIES)}) "
        f"AND rt.name IN ({','.join(quote_literal(name) for name in REQUIRED_ENTITIES)}) "
        "ORDER BY pt.name, fk.name, fkc.constraint_column_id;"
    )
    fk_headers, fk_rows = _normalized_headers(sqlcmd(fk_query, password, database=DATABASE))
    for row in fk_rows:
        fk.append(tuple(row[field_index(fk_headers, key)] for key in ("parent_table", "parent_column", "referenced_table", "referenced_column")))
    missing = [name for name in REQUIRED_ENTITIES if name not in objects]
    no_primary_key = [name for name in REQUIRED_ENTITIES if name in objects and objects[name][1] == "USER_TABLE" and not pk[name]]
    return {"objects": objects, "counts": counts, "primary_keys": pk, "foreign_keys": fk, "missing": missing, "no_primary_key": no_primary_key}


def write_summary(summary: dict[str, Any]) -> None:
    entities = summary["entities"]
    lines = [
        "## Folketinget backup restore PoC",
        "",
        f"- Backup bytes: {summary['backup_size_bytes']}",
        f"- SHA-256: {summary['download']['sha256']}",
        f"- Download seconds: {summary['download']['download_seconds']}",
        f"- Backup SQL major: {summary['header']['source_sql_major']}",
        f"- Backup finish: {summary['header']['backup_finish_date']}",
        f"- Backup compressed: {summary['header']['compressed']}",
        f"- Restored database bytes: {summary['restored_database_size_bytes']}",
        "- DBCC CHECKDB: PASS",
        "",
        "| Entity | Present | Rows | Primary key columns |",
        "|---|---:|---:|---|",
    ]
    for name in REQUIRED_ENTITIES:
        present = name in entities["objects"]
        key_columns = ", ".join(entities["primary_keys"].get(name, [])) or "none reported"
        lines.append(f"| {name} | {'yes' if present else 'no'} | {entities['counts'].get(name, '—')} | {key_columns} |")
    lines.extend(["", "Relationship/FK metadata:"])
    if entities["foreign_keys"]:
        lines.extend(f"- {source}.{source_col} → {target}.{target_col}" for source, source_col, target, target_col in entities["foreign_keys"])
    else:
        lines.append("- No declared foreign keys found between the six entities.")
    lines.extend(["", "No rows, IDs, or personal data were included."])
    text = "\n".join(lines) + "\n"
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as output:
            output.write(text)
    print(text, end="")


def cleanup(password: str | None, backup_dir: Path | None, data_dir: Path | None) -> None:
    cleanup_failed = False
    if password:
        service = subprocess.run(["sudo", "systemctl", "is-active", "--quiet", "mssql-server"], capture_output=True, check=False, timeout=20)
        if service.returncode == 0:
            try:
                sqlcmd(
                    f"IF DB_ID({quote_literal(DATABASE)}) IS NOT NULL BEGIN ALTER DATABASE {quote_identifier(DATABASE)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE {quote_identifier(DATABASE)}; END;",
                    password, timeout=60,
                )
            except PocError:
                cleanup_failed = True
    for path in (backup_dir, data_dir):
        if path is not None:
            resolved = path.resolve()
            backup_root = Path(os.environ.get("RUNNER_TEMP", "/tmp")).resolve()
            allowed_backup = resolved.parent == backup_root and resolved.name.startswith("denmarkbot-poc-backup-")
            allowed_data = resolved.parent == Path("/var/opt/mssql/data").resolve() and resolved.name.startswith("denmarkbot-poc-data-")
            if not (allowed_backup or allowed_data):
                cleanup_failed = True
                continue
            result = subprocess.run(["sudo", "rm", "-rf", "--", str(path)], capture_output=True, check=False, timeout=60)
            cleanup_failed = cleanup_failed or result.returncode != 0
    if cleanup_failed:
        raise PocError("temporary_data_cleanup_failed")


def main() -> int:
    password: str | None = None
    backup_dir: Path | None = None
    data_dir: Path | None = None
    try:
        validate_confirmation(os.environ.get("INPUT_CONFIRM_RESTORE", ""))
        username = os.environ.get("ODA_BACKUP_USERNAME", "")
        credential = os.environ.get("ODA_BACKUP_PASSWORD", "")
        validate_credentials(username, credential)
        release = platform.freedesktop_os_release().get("VERSION_ID", "")
        require_x64_ubuntu24(platform.machine(), release)
        runner_temp = Path(os.environ.get("RUNNER_TEMP", "/tmp")).resolve()
        initial_free = free_bytes(runner_temp)
        print(f"runner_arch=x64 ubuntu={release} free_disk_before_install_bytes={initial_free}")
        check_free_space(initial_free, 8 * GIB)
        password = __import__("secrets").token_urlsafe(32) + "Aa1!"
        run_sql_server_setup(password)
        after_install = free_bytes(runner_temp)
        print(f"sql_server_edition=Express 2025 free_disk_before_download_bytes={after_install}")
        opener = make_oda_opener(username, credential)
        content_length = request_content_length(opener)
        check_free_space(after_install, download_space_required(content_length))
        run_id = re.sub(r"[^A-Za-z0-9-]", "", os.environ.get("GITHUB_RUN_ID", "manual")) or "manual"
        suffix = __import__("secrets").token_hex(6)
        backup_dir = runner_temp / f"denmarkbot-poc-backup-{run_id}-{suffix}"
        data_dir = Path("/var/opt/mssql/data") / f"denmarkbot-poc-data-{run_id}-{suffix}"
        run_checked(["sudo", "install", "-d", "-o", os.environ.get("USER", "runner"), "-g", "mssql", "-m", "0750", str(backup_dir)], "backup_temp_setup_failed")
        backup_path = backup_dir / "oda.bak"
        download = download_backup(opener, backup_path, content_length)
        print(f"backup_bytes={download['file_size_bytes']} sha256={download['sha256']} download_seconds={download['download_seconds']}")
        restore_parent = Path("/var/opt/mssql/data")
        restore_free = free_bytes(restore_parent)
        print(f"free_disk_before_restore_bytes={restore_free}")
        # RESTORE FILELISTONLY is checked before any database files are created.
        header_output = sqlcmd(f"RESTORE HEADERONLY FROM DISK = {quote_literal(str(backup_path))};", password, timeout=600)
        header_info = header_metadata(header_output)
        file_output = sqlcmd(f"RESTORE FILELISTONLY FROM DISK = {quote_literal(str(backup_path))};", password, timeout=600)
        entries = filelist_metadata(file_output)
        check_free_space(restore_free, restore_space_required([int(item["size"]) for item in entries]))
        run_checked(["sudo", "install", "-d", "-o", "mssql", "-g", "mssql", "-m", "0750", str(data_dir)], "restore_directory_setup_failed")
        restore_query = build_restore_query(str(backup_path), str(data_dir), entries)
        sqlcmd(restore_query, password, timeout=20 * 60)
        checkdb = sqlcmd(f"DBCC CHECKDB ({quote_identifier(DATABASE)}) WITH ALL_ERRORMSGS;", password, timeout=20 * 60)
        if not re.search(r"0 allocation errors and 0 consistency errors", checkdb, re.IGNORECASE):
            raise PocError("dbcc_checkdb_not_clean")
        restored_size = int(simple_value(sqlcmd("SELECT CAST(SUM(size) * 8192 AS bigint) FROM sys.database_files;", password, database=DATABASE)))
        entities = validate_entities(password)
        summary = {
            "download": download,
            "backup_size_bytes": download["file_size_bytes"],
            "header": header_info,
            "restored_database_size_bytes": restored_size,
            "entities": entities,
        }
        write_summary(summary)
        if entities["missing"] or entities["no_primary_key"]:
            raise PocError("required_entity_validation_failed")
        print("restore_validation=PASS")
    except PocError as exc:
        print(f"restore_validation=FAIL category={exc.category}", file=sys.stderr)
        return_code = 2
    except Exception:
        print("restore_validation=FAIL category=unexpected_internal_error", file=sys.stderr)
        return_code = 2
    else:
        return_code = 0
    finally:
        os.environ.pop("ODA_BACKUP_USERNAME", None)
        os.environ.pop("ODA_BACKUP_PASSWORD", None)
        try:
            cleanup(password, backup_dir, data_dir)
        except PocError as exc:
            print(f"cleanup=FAIL category={exc.category}", file=sys.stderr)
            return_code = 2
        if password is not None:
            password = None
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
