#!/usr/bin/env python3
"""Manual-only, read-only restore validation for the official ODA backup."""

from __future__ import annotations

import base64
import binascii
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

TOOLS_DIRECTORY = str(Path(__file__).resolve().parent)
if TOOLS_DIRECTORY not in sys.path:
    sys.path.insert(0, TOOLS_DIRECTORY)
import folketinget_snapshot_manifest as snapshot_manifest

ODA_URL = "https://oda.ft.dk/odapublish/oda.bak"
ODA_ORIGIN = ("https", "oda.ft.dk", 443)
REQUIRED_ENTITIES = ("Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus")
MAX_BACKUP_BYTES = 5 * 1024**3
GIB = 1024**3
SQL_MAJOR = 17  # SQL Server 2025
SQLCMD = "/opt/mssql-tools18/bin/sqlcmd"
DATABASE = "DenmarkBotFolketingetBackupPoc"
BACKUP_ROOT = Path("/var/opt/mssql/backup")
MANIFEST_DIRECTORY_NAME = "denmarkbot-folketinget-manifest"
MANIFEST_FILENAME = "manifest.sqlite"
RESTORE_VALIDATE = "RESTORE_VALIDATE"
CREATE_MANIFEST_BASELINE = "CREATE_MANIFEST_BASELINE"
VALIDATE_SQL_PROVIDER = "VALIDATE_SQL_PROVIDER"
MIN_MANIFEST_COMPATIBILITY_LEVEL = 110
SUPPORTED_COMPATIBILITY_LEVELS = frozenset({100, 110, 120, 130, 140, 150, 160, 170})
MANIFEST_BASE64_CHUNK_SIZE = 6000
# 64 MiB UTF-8 JSON needs at most 128 MiB UTF-16LE, or 29,827 chunks.
MANIFEST_MAX_BASE64_CHUNKS = 30000
# Five sequence digits + one delimiter + 6000 ASCII bytes = at most 6006 columns.
MANIFEST_SQL_ROW_WIDTH = 6100


class PocError(Exception):
    """Safe error category; messages must not contain external values."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def validate_confirmation(value: str) -> None:
    if value != "true":
        raise PocError("manual_confirmation_required")


def validate_mode(value: str) -> str:
    if value not in {RESTORE_VALIDATE, CREATE_MANIFEST_BASELINE, VALIDATE_SQL_PROVIDER}:
        raise PocError("workflow_mode_invalid")
    return value


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


def parse_df_available_bytes(output: str) -> int:
    """Parse only GNU df's available-byte column, with a stable C locale."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 2 or lines[0] != "Avail" or not re.fullmatch(r"[0-9]+", lines[1]):
        raise PocError("restore_disk_space_result_invalid")
    try:
        available = int(lines[1], 10)
    except (ValueError, OverflowError):
        raise PocError("restore_disk_space_result_invalid") from None
    if available < 0:
        raise PocError("restore_disk_space_result_invalid")
    return available


def free_bytes(path: Path) -> int:
    # Query the containing filesystem as root: the runner may not have search
    # permission on SQL Server's data directory. Never expose df output.
    output = run_checked(
        ["sudo", "df", "--output=avail", "-B1", "--", str(path)],
        "restore_disk_space_check_failed",
        env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        timeout=15,
    )
    return parse_df_available_bytes(output)


def validate_download_metadata(download: dict[str, object], expected_bytes: int) -> tuple[int, str, float]:
    try:
        size = int(download["file_size_bytes"])
        checksum = str(download["sha256"])
        duration = float(download["download_seconds"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise PocError("backup_download_metadata_invalid") from None
    if size != expected_bytes or len(checksum) != 64 or duration < 0:
        raise PocError("backup_download_metadata_invalid")
    if any(character not in "0123456789abcdef" for character in checksum.lower()):
        raise PocError("backup_download_metadata_invalid")
    return size, checksum, duration


def check_restore_disk_space(data_dir: Path, required_bytes: int) -> int:
    try:
        available = free_bytes(data_dir)
    except PocError:
        raise
    except OSError:
        raise PocError("restore_disk_space_check_failed") from None
    check_free_space(available, required_bytes)
    return available


def validate_backup_readable_by_sql_server(path: Path, expected_bytes: int) -> None:
    size_output = run_checked(
        ["sudo", "-u", "mssql", "stat", "-c", "%F %a %s", "--", str(path)],
        "backup_file_validation_failed",
        timeout=15,
    )
    match = re.fullmatch(r"\s*regular file ([0-7]{3,4}) ([0-9]+)\s*", size_output)
    if match is None:
        raise PocError("backup_file_validation_failed")
    mode, size = int(match.group(1), 8), int(match.group(2), 10)
    if size != expected_bytes or mode & 0o007:
        raise PocError("backup_file_validation_failed")
    run_checked(
        ["sudo", "-u", "mssql", "test", "-r", str(path)],
        "backup_path_unreadable_by_sql_server", timeout=15,
    )


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


def sqlcmd(
    query: str,
    password: str,
    *,
    timeout: int = 900,
    database: str = "master",
    failure_category: str = "sql_server_command_failed",
) -> str:
    return run_checked(
        [SQLCMD, "-S", "localhost", "-U", "sa", "-C", "-b", "-W", "-s", "|", "-w", "65535", "-d", database, "-Q", query],
        failure_category, env=sql_env(password), timeout=timeout,
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
    try:
        major = int(row[field_index(headers, "SoftwareVersionMajor")])
        backup_type = int(row[field_index(headers, "BackupType")])
        damaged_value = row[field_index(headers, "IsDamaged")].lower()
    except (IndexError, TypeError, ValueError, OverflowError):
        raise PocError("backup_header_invalid") from None
    validate_backup_header(major, backup_type, damaged_value in {"1", "true"})
    return {
        "source_sql_major": major,
        "backup_type": backup_type,
        "compressed": row[field_index(headers, "Compressed")],
        "backup_finish_date": row[field_index(headers, "BackupFinishDate")],
    }


def filelist_metadata(output: str) -> list[dict[str, object]]:
    headers, rows = _normalized_headers(output)
    entries: list[dict[str, object]] = []
    for row in rows:
        file_type = row[field_index(headers, "Type")]
        if file_type not in {"D", "L"}:
            raise PocError("backup_file_type_unsupported")
        try:
            size = int(row[field_index(headers, "Size")])
            logical_name = row[field_index(headers, "LogicalName")]
        except (IndexError, TypeError, ValueError, OverflowError):
            raise PocError("backup_filelist_invalid") from None
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
        "SELECT s.name AS schema_name, o.name AS entity_name, o.type_desc AS object_type FROM sys.objects AS o "
        "JOIN sys.schemas AS s ON s.schema_id=o.schema_id "
        f"WHERE o.type IN ('U','V') AND o.name IN ({names}) ORDER BY o.name,s.name;"
    )
    output = sqlcmd(query, password, database=DATABASE, failure_category="entity_discovery_query_failed")
    return parse_entity_discovery(output)


def parse_entity_discovery(output: str) -> dict[str, tuple[str, str]]:
    try:
        headers, rows = _normalized_headers(output)
        schema_index = field_index(headers, "schema_name")
        entity_index = field_index(headers, "entity_name")
        type_index = field_index(headers, "object_type")
        found: dict[str, tuple[str, str]] = {}
        for row in rows:
            schema, name, object_type = row[schema_index], row[entity_index], row[type_index]
            if not schema or name not in REQUIRED_ENTITIES or object_type not in {"USER_TABLE", "VIEW"}:
                raise PocError("entity_discovery_result_invalid")
            if name in found:
                raise PocError("entity_mapping_ambiguous")
            found[name] = (schema, object_type)
        return found
    except PocError as exc:
        if exc.category in {"entity_mapping_ambiguous", "entity_discovery_result_invalid"}:
            raise
        raise PocError("entity_discovery_result_invalid") from None
    except (IndexError, TypeError, ValueError):
        raise PocError("entity_discovery_result_invalid") from None


def _nonnegative_sql_integer(output: str, category: str, *, allow_zero: bool) -> int:
    try:
        value = int(simple_value(output), 10)
    except (PocError, TypeError, ValueError, OverflowError):
        raise PocError(category) from None
    if value < 0 or (not allow_zero and value == 0):
        raise PocError(category)
    return value


def parse_restored_database_size(output: str) -> int:
    return _nonnegative_sql_integer(output, "restored_database_size_invalid", allow_zero=False)


def restored_database_size(password: str) -> int:
    output = sqlcmd(
        "SELECT SUM(CAST(size AS bigint)) * CAST(8192 AS bigint) FROM sys.database_files;",
        password,
        database=DATABASE,
        failure_category="restored_database_size_query_failed",
    )
    return parse_restored_database_size(output)


def parse_database_compatibility_level(output: str) -> int:
    numeric_values = [line.strip() for line in output.splitlines() if re.fullmatch(r"\s*[0-9]{3}\s*", line)]
    try:
        value = simple_value(output)
    except PocError:
        raise PocError("manifest_database_compatibility_invalid") from None
    if len(numeric_values) != 1 or value != numeric_values[0]:
        raise PocError("manifest_database_compatibility_invalid")
    level = int(value, 10)
    if level not in SUPPORTED_COMPATIBILITY_LEVELS:
        raise PocError("manifest_database_compatibility_invalid")
    return level


def database_compatibility_level(password: str) -> int:
    output = sqlcmd(
        "SELECT compatibility_level FROM sys.databases WHERE database_id=DB_ID();",
        password,
        database=DATABASE,
        failure_category="manifest_database_compatibility_query_failed",
    )
    return parse_database_compatibility_level(output)


def validate_manifest_compatibility_level(level: int) -> None:
    if level not in SUPPORTED_COMPATIBILITY_LEVELS:
        raise PocError("manifest_database_compatibility_invalid")
    if level < MIN_MANIFEST_COMPATIBILITY_LEVEL:
        raise PocError("manifest_database_compatibility_unsupported")


def report_manifest_compatibility(level: int) -> None:
    if level not in SUPPORTED_COMPATIBILITY_LEVELS:
        raise PocError("manifest_database_compatibility_invalid")
    print(f"progress=manifest_database_compatibility level={level}")


def parse_entity_count(output: str) -> int:
    return _nonnegative_sql_integer(output, "entity_count_result_invalid", allow_zero=True)


def parse_primary_key_metadata(output: str) -> list[str]:
    try:
        headers, rows = _normalized_headers(output)
        column_index = field_index(headers, "column_name")
        names = [row[column_index] for row in rows]
        if any(not name for name in names):
            raise PocError("primary_key_metadata_invalid")
        return names
    except PocError:
        raise PocError("primary_key_metadata_invalid") from None
    except (IndexError, TypeError, ValueError):
        raise PocError("primary_key_metadata_invalid") from None


def parse_foreign_key_metadata(output: str) -> list[tuple[str, str, str, str]]:
    columns = ("parent_table", "parent_column", "referenced_table", "referenced_column")
    try:
        headers, rows = _normalized_headers(output)
        indexes = [field_index(headers, column) for column in columns]
        relations = [tuple(row[index] for index in indexes) for row in rows]
        if any(any(not value for value in relation) for relation in relations):
            raise PocError("foreign_key_metadata_invalid")
        return relations
    except PocError:
        raise PocError("foreign_key_metadata_invalid") from None
    except (IndexError, TypeError, ValueError):
        raise PocError("foreign_key_metadata_invalid") from None


def validate_entities(password: str) -> dict[str, Any]:
    print("progress=entity_discovery")
    objects = table_objects(password)
    counts: dict[str, int] = {}
    pk: dict[str, list[str]] = {name: [] for name in REQUIRED_ENTITIES}
    print("progress=entity_counts")
    for name, (schema, object_type) in objects.items():
        full_name = f"{quote_identifier(schema)}.{quote_identifier(name)}"
        count_output = sqlcmd(
            f"SELECT COUNT_BIG(*) FROM {full_name};",
            password,
            database=DATABASE,
            timeout=600,
            failure_category="entity_count_query_failed",
        )
        counts[name] = parse_entity_count(count_output)
    print("progress=primary_key_metadata")
    for name, (schema, object_type) in objects.items():
        if object_type == "USER_TABLE":
            pk_query = (
                "SELECT ic.key_ordinal AS key_ordinal, c.name AS column_name FROM sys.indexes AS i "
                "JOIN sys.index_columns AS ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id "
                "JOIN sys.columns AS c ON c.object_id=ic.object_id AND c.column_id=ic.column_id "
                f"WHERE i.is_primary_key=1 AND i.object_id=OBJECT_ID({quote_literal(schema + '.' + name)}) "
                "ORDER BY ic.key_ordinal;"
            )
            pk_output = sqlcmd(
                pk_query,
                password,
                database=DATABASE,
                failure_category="primary_key_metadata_query_failed",
            )
            pk[name] = parse_primary_key_metadata(pk_output)
    print("progress=foreign_key_metadata")
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
    fk_output = sqlcmd(
        fk_query,
        password,
        database=DATABASE,
        failure_category="foreign_key_metadata_query_failed",
    )
    fk = parse_foreign_key_metadata(fk_output)
    missing = [name for name in REQUIRED_ENTITIES if name not in objects]
    no_primary_key = [name for name in REQUIRED_ENTITIES if name in objects and objects[name][1] == "USER_TABLE" and not pk[name]]
    return {"objects": objects, "counts": counts, "primary_keys": pk, "foreign_keys": fk, "missing": missing, "no_primary_key": no_primary_key}


def manifest_schema(password: str, entity: str, schema_name: str) -> dict[str, Any]:
    if entity not in REQUIRED_ENTITIES:
        raise PocError("manifest_schema_invalid")
    query = (
        "SELECT c.column_id AS [ordinal], c.name AS [column_name], t.name AS [type_name], "
        "c.max_length AS [max_length], c.precision AS [precision], c.scale AS [scale], "
        "c.is_nullable AS [nullable], ISNULL(pk.key_ordinal,0) AS [key_ordinal], "
        "ISNULL(c.collation_name,'') AS [collation] "
        "FROM sys.columns AS c JOIN sys.types AS t ON t.user_type_id=c.user_type_id "
        "JOIN sys.objects AS o ON o.object_id=c.object_id "
        "JOIN sys.schemas AS s ON s.schema_id=o.schema_id "
        "LEFT JOIN (SELECT ic.object_id,ic.column_id,ic.key_ordinal FROM sys.index_columns AS ic "
        "JOIN sys.indexes AS i ON i.object_id=ic.object_id AND i.index_id=ic.index_id "
        "WHERE i.is_primary_key=1 AND ic.key_ordinal>0) AS pk "
        "ON pk.object_id=c.object_id AND pk.column_id=c.column_id "
        f"WHERE s.name={quote_literal(schema_name)} AND o.name={quote_literal(entity)} "
        "AND o.type='U' ORDER BY c.column_id;"
    )
    output = sqlcmd(
        query, password, database=DATABASE, failure_category="manifest_schema_query_failed"
    )
    try:
        headers, rows = _normalized_headers(output)
        indexes = {name: field_index(headers, name) for name in (
            "ordinal", "column_name", "type_name", "max_length", "precision", "scale",
            "nullable", "key_ordinal", "collation",
        )}
        columns = []
        for row in rows:
            columns.append({
                "ordinal": int(row[indexes["ordinal"]]),
                "name": row[indexes["column_name"]],
                "type_name": row[indexes["type_name"]],
                "max_length": int(row[indexes["max_length"]]),
                "precision": int(row[indexes["precision"]]),
                "scale": int(row[indexes["scale"]]),
                "nullable": int(row[indexes["nullable"]]),
                "key_ordinal": int(row[indexes["key_ordinal"]]),
                "collation": row[indexes["collation"]] or None,
            })
        return {"schema_name": schema_name, "object_type": "USER_TABLE", "columns": columns}
    except (PocError, IndexError, TypeError, ValueError, OverflowError):
        raise PocError("manifest_schema_invalid") from None


def manifest_page_json(
    password: str, entity: str, schema_name: str, schema: dict[str, Any], offset: int, limit: int
) -> str:
    if entity not in REQUIRED_ENTITIES or offset < 0 or limit <= 0 or limit > snapshot_manifest.DEFAULT_BATCH_SIZE:
        raise PocError("manifest_page_invalid")
    columns = schema["columns"]
    selected_expressions = []
    for index, column in enumerate(columns):
        identifier = quote_identifier(column["name"])
        sql_type = str(column["type_name"]).casefold()
        if sql_type == "text":
            expression = f"CONVERT(varchar(max), {identifier})"
        elif sql_type == "ntext":
            expression = f"CONVERT(nvarchar(max), {identifier})"
        else:
            expression = identifier
        selected_expressions.append(f"{expression} AS {quote_identifier(f'c{index:04d}')}")
    selected = ", ".join(selected_expressions)
    primary_key = schema["primary_key"]
    ordering = ", ".join(quote_identifier(name) for name in primary_key)
    select = (
        f"SELECT {selected} FROM {quote_identifier(schema_name)}.{quote_identifier(entity)} "
        f"ORDER BY {ordering} OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY "
        "FOR JSON PATH, INCLUDE_NULL_VALUES"
    )
    query = build_chunked_json_query(select, variable="manifest")
    output = run_manifest_page_sql(
        [SQLCMD, "-S", "localhost", "-U", "sa", "-C", "-b", "-r",
         "-h", "-1", "-W", "-s", "|", "-w", str(MANIFEST_SQL_ROW_WIDTH),
         "-d", DATABASE, "-Q", query],
        password,
        entity=entity,
        offset=offset,
        timeout=600,
    )
    payload = parse_manifest_page_output(output, entity=entity, offset=offset)
    try:
        if len(payload.encode("utf-8")) > snapshot_manifest.MAX_BATCH_JSON_BYTES:
            raise PocError("manifest_page_too_large")
    except UnicodeEncodeError:
        raise PocError("manifest_page_invalid") from None
    return payload


def build_chunked_json_query(select_query: str, *, variable: str) -> str:
    """Wrap a trusted JSON SELECT in the validated fixed-width Base64 transport."""
    if variable not in {"manifest", "provider"} or not select_query.startswith("SELECT "):
        raise PocError("manifest_page_invalid")
    json_variable = f"@{variable}_json"
    b64_variable = f"@{variable}_b64"
    return (
        f"SET NOCOUNT ON; DECLARE {json_variable} nvarchar(max); "
        f"SET {json_variable} = ({select_query}); "
        f"DECLARE {b64_variable} varchar(max); "
        f"SET {b64_variable} = BASE64_ENCODE(CONVERT(varbinary(max), {json_variable})); "
        ";WITH chunk_sequence AS ("
        "SELECT CAST(1 AS int) AS sequence_number UNION ALL "
        f"SELECT sequence_number + 1 FROM chunk_sequence WHERE sequence_number * {MANIFEST_BASE64_CHUNK_SIZE} < LEN({b64_variable})) "
        "SELECT CAST(CAST(sequence_number AS varchar(5)) + '|' + "
        f"CAST(SUBSTRING({b64_variable}, ((sequence_number - 1) * {MANIFEST_BASE64_CHUNK_SIZE}) + 1, "
        f"{MANIFEST_BASE64_CHUNK_SIZE}) AS varchar({MANIFEST_BASE64_CHUNK_SIZE})) AS varchar(6006)) AS chunk_line "
        f"FROM chunk_sequence ORDER BY sequence_number OPTION (MAXRECURSION {MANIFEST_MAX_BASE64_CHUNKS});"
    )


def run_provider_chunked_json(password: str, query: str, entity: str) -> str:
    """Execute provider JSON through the same strict manifest chunk transport."""
    if entity not in REQUIRED_ENTITIES:
        raise PocError("manifest_page_invalid")
    argv = [SQLCMD, "-S", "localhost", "-U", "sa", "-C", "-b", "-r",
            "-h", "-1", "-W", "-s", "|", "-w", str(MANIFEST_SQL_ROW_WIDTH),
            "-d", DATABASE, "-Q", query]
    output = run_manifest_page_sql(argv, password, entity=entity, offset=0, timeout=600)
    return parse_manifest_page_output(output, entity=entity, offset=0)


def parse_manifest_page_output(output: str, *, entity: str, offset: int) -> str:
    """Reassemble strict sequence-numbered Base64 chunks from sqlcmd rows."""
    if entity not in REQUIRED_ENTITIES or offset < 0:
        raise PocError("manifest_page_invalid")
    lines = [line.removesuffix("\r") for line in output.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    chunk_count = len(lines)
    encoded_chars = 0

    def fail(stage: str, category: str) -> None:
        print(
            f"manifest_page_parse_failure entity={entity} offset={offset} "
            f"chunk_count={chunk_count} encoded_chars={encoded_chars} stage={stage} category={category}",
            file=sys.stderr,
        )
        raise PocError(category)

    if not lines or chunk_count > MANIFEST_MAX_BASE64_CHUNKS:
        fail("sequence", "manifest_page_chunk_sequence_invalid")
    chunks: list[str] = []
    for expected_sequence, line in enumerate(lines, start=1):
        parts = line.split("|")
        if len(parts) != 2 or not re.fullmatch(r"[1-9][0-9]{0,4}", parts[0]):
            fail("row", "manifest_page_chunk_row_invalid")
        sequence = int(parts[0], 10)
        chunk = parts[1]
        encoded_chars += len(chunk)
        if sequence != expected_sequence:
            fail("sequence", "manifest_page_chunk_sequence_invalid")
        if not chunk or len(chunk) > MANIFEST_BASE64_CHUNK_SIZE or len(chunk) % 4:
            fail("base64_format", "manifest_page_base64_format_invalid")
        if not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", chunk):
            fail("base64_format", "manifest_page_base64_format_invalid")
        if expected_sequence < chunk_count:
            if len(chunk) != MANIFEST_BASE64_CHUNK_SIZE or "=" in chunk:
                fail("base64_format", "manifest_page_base64_format_invalid")
        chunks.append(chunk)
    encoded = "".join(chunks)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        fail("base64_decode", "manifest_page_base64_decode_failed")
    if base64.b64encode(raw).decode("ascii") != encoded:
        fail("base64_decode", "manifest_page_base64_decode_failed")
    try:
        payload = raw.decode("utf-16-le", errors="strict")
    except UnicodeDecodeError:
        fail("utf16_decode", "manifest_page_utf16_decode_failed")
    try:
        if len(payload.encode("utf-8", errors="strict")) > snapshot_manifest.MAX_BATCH_JSON_BYTES:
            fail("size", "manifest_page_too_large")
        snapshot_manifest._decode_json_page(payload)
    except UnicodeEncodeError:
        fail("utf16_decode", "manifest_page_utf16_decode_failed")
    except snapshot_manifest.ManifestError:
        fail("json", "manifest_page_json_invalid")
    return payload


def run_manifest_page_sql(
    argv: list[str], password: str, *, entity: str, offset: int, timeout: int
) -> str:
    """Run one bounded page query and expose only safe SQL diagnostics."""
    if entity not in REQUIRED_ENTITIES or offset < 0:
        raise PocError("manifest_page_invalid")
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=sql_env(password),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        print(
            f"manifest_page_sql_failure entity={entity} offset={offset} "
            "return_code=timeout category=manifest_page_sqlcmd_failed",
            file=sys.stderr,
        )
        raise PocError("manifest_page_sqlcmd_failed") from None
    except OSError:
        print(
            f"manifest_page_sql_failure entity={entity} offset={offset} "
            "return_code=unavailable category=manifest_page_sqlcmd_failed",
            file=sys.stderr,
        )
        raise PocError("manifest_page_sqlcmd_failed") from None
    if result.returncode != 0:
        # Never expose either stream: stdout may contain selected row values.
        # -r routes SQL errors to stderr; stdout is a safe numeric fallback only.
        msg_pattern = re.compile(r"(?im)^\s*Msg\s+([0-9]{1,6})\b")
        match = msg_pattern.search(result.stderr or "")
        if match is None:
            match = msg_pattern.search(result.stdout or "")
        category = f"manifest_page_sql_msg_{match.group(1)}" if match else "manifest_page_sqlcmd_failed"
        print(
            f"manifest_page_sql_failure entity={entity} offset={offset} "
            f"return_code={result.returncode} category={category}",
            file=sys.stderr,
        )
        raise PocError(category)
    return result.stdout


def report_manifest_entity(entity: str) -> None:
    if entity not in REQUIRED_ENTITIES:
        raise PocError("manifest_page_invalid")
    print(f"progress=manifest_entity entity={entity}")


def read_sql_provider_sample_ids(password: str, entities: dict[str, Any]) -> dict[str, int]:
    """Select one deterministic internal ID from each validated table."""
    samples: dict[str, int] = {}
    try:
        for entity in REQUIRED_ENTITIES:
            schema, object_type = entities["objects"][entity]
            if object_type != "USER_TABLE" or entities["primary_keys"].get(entity) != ["id"]:
                raise PocError("sql_provider_sample_unavailable")
            table = f"{quote_identifier(schema)}.{quote_identifier(entity)}"
            output = sqlcmd(
                f"SELECT MIN({quote_identifier('id')}) AS [sample_id] FROM {table};",
                password,
                database=DATABASE,
                failure_category="sql_provider_sample_query_failed",
            )
            value = simple_value(output)
            if not re.fullmatch(r"[0-9]+", value):
                raise PocError("sql_provider_sample_unavailable")
            samples[entity] = int(value, 10)
    except PocError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError):
        raise PocError("sql_provider_sample_unavailable") from None
    return samples


def exercise_sql_provider(provider: Any, sample_ids: dict[str, int]) -> dict[str, int]:
    """Exercise by-ID and relationship reads; return aggregate-only results."""
    from collectors.folketinget_models import normalize_many

    if set(sample_ids) != set(REQUIRED_ENTITIES) or any(
        isinstance(value, bool) or not isinstance(value, int) for value in sample_ids.values()
    ):
        raise PocError("sql_provider_validation_failed")
    failures = 0
    sample_rows: dict[str, dict[str, Any]] = {}

    def validate_rows(entity: str, rows: Any) -> tuple[dict[str, Any], ...]:
        nonlocal failures
        result = normalize_many(list(rows), entity)
        failures += len(result.diagnostics)
        if not result.complete or len(result.record or ()) != len(rows):
            failures += 1
        return tuple(row for row in rows if isinstance(row, dict))

    try:
        get_checks = 0
        for entity in REQUIRED_ENTITIES:
            rows = tuple(provider.get_by_ids(entity, (sample_ids[entity],)))
            if len(rows) != 1 or rows[0].get("id") != sample_ids[entity]:
                raise PocError("sql_provider_validation_failed")
            normalized = validate_rows(entity, rows)
            if len(normalized) != 1:
                raise PocError("sql_provider_validation_failed")
            sample_rows[entity] = normalized[0]
            get_checks += 1

        relations: list[tuple[str, str, object]] = []
        sag_id = sample_rows["Sag"].get("id")
        status_id = sample_rows["Sag"].get("statusid")
        if isinstance(sag_id, int) and not isinstance(sag_id, bool):
            relations.extend((("Sagstrin", "sagid", sag_id), ("SagDokument", "sagid", sag_id)))
        if isinstance(status_id, int) and not isinstance(status_id, bool):
            relations.append(("Sag", "statusid", status_id))
        document_id = sample_rows["Dokument"].get("id")
        if isinstance(document_id, int) and not isinstance(document_id, bool):
            relations.append(("SagDokument", "dokumentid", document_id))
        stage_id = sample_rows["Sagstrin"].get("id")
        if isinstance(stage_id, int) and not isinstance(stage_id, bool):
            relations.append(("Afstemning", "sagstrinid", stage_id))
        source_status_id = sample_rows["Sagsstatus"].get("id")
        if isinstance(source_status_id, int) and not isinstance(source_status_id, bool):
            relations.append(("Sag", "statusid", source_status_id))

        for entity, field, value in relations:
            rows = tuple(provider.find_by_field(entity, field, (value,)))
            validate_rows(entity, rows)
        if failures:
            raise PocError("sql_provider_normalization_failed")
        return {"entities_tested": len(REQUIRED_ENTITIES), "get_by_ids_checks": get_checks,
                "relation_checks": len(relations), "normalization_failures": failures}
    except PocError:
        raise
    except Exception:
        raise PocError("sql_provider_validation_failed") from None


def validate_sql_provider(password: str, entities: dict[str, Any]) -> dict[str, int]:
    from folketinget_sql_provider import SQLServerFolketingetProvider

    samples = read_sql_provider_sample_ids(password, entities)
    try:
        column_types: dict[str, dict[str, str]] = {}
        for entity in REQUIRED_ENTITIES:
            schema_name, object_type = entities["objects"][entity]
            schema = manifest_schema(password, entity, schema_name)
            available_types = {column["name"]: column["type_name"] for column in schema["columns"]}
            from folketinget_sql_provider import PROJECTIONS

            if set(PROJECTIONS[entity]) - set(available_types):
                raise PocError("sql_provider_schema_invalid")
            column_types[entity] = {name: available_types[name] for name in PROJECTIONS[entity]}
        provider = SQLServerFolketingetProvider(
            entities["objects"],
            lambda query, entity: run_provider_chunked_json(password, query, entity),
            column_types=column_types,
        )
    except PocError:
        raise
    except Exception:
        raise PocError("sql_provider_validation_failed") from None
    return exercise_sql_provider(provider, samples)


def write_summary(summary: dict[str, Any]) -> None:
    entities = summary["entities"]
    primary_key_tables = sum(bool(columns) for columns in entities["primary_keys"].values())
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
        f"- Required entities present: {len(entities['objects'])}/{len(REQUIRED_ENTITIES)}",
        f"- Rows across required entities: {sum(entities['counts'].values())}",
        f"- Entities with primary-key metadata: {primary_key_tables}",
        f"- Declared relationships among required entities: {len(entities['foreign_keys'])}",
        "",
    ]
    lines.append("No table/column names, rows, IDs, or personal data were included.")
    manifest = summary.get("manifest")
    if manifest is not None:
        lines.extend([
            "",
            "## Snapshot manifest",
            "",
            f"- Manifest SHA-256: {manifest['sha256']}",
            f"- Manifest bytes: {manifest['manifest_bytes']}",
            f"- Total manifest rows: {manifest['total_rows']}",
            f"- Schema fingerprint: {manifest['schema_fingerprint']}",
            f"- Manifest validation: {manifest['validation']}",
            "- Per-entity row counts:",
        ])
        lines.extend(f"  - {entity}: {count}" for entity, count in manifest["entity_counts"].items())
    text = "\n".join(lines) + "\n"
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as output:
            output.write(text)
    print(text, end="")


def cleanup_path_is_allowed(
    path: Path,
    runner_temp: Path,
    backup_root: Path = BACKUP_ROOT,
) -> bool:
    """Allow removal only of named, direct child directories created by this PoC."""
    if not path.is_absolute() or ".." in path.parts:
        return False
    if path.parent == backup_root and path.name.startswith("denmarkbot-poc-backup-"):
        return True
    if path.parent == runner_temp and path.name.startswith("denmarkbot-poc-download-"):
        return True
    data_root = Path("/var/opt/mssql/data")
    return path.parent == data_root and path.name.startswith("denmarkbot-poc-data-")


def cleanup(
    password: str | None,
    backup_dir: Path | None,
    data_dir: Path | None,
    download_dir: Path | None = None,
) -> None:
    cleanup_failed = False
    if password:
        try:
            service = subprocess.run(["sudo", "systemctl", "is-active", "--quiet", "mssql-server"], capture_output=True, check=False, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            cleanup_failed = True
            service = None
        if service is not None and service.returncode == 0:
            try:
                sqlcmd(
                    f"IF DB_ID({quote_literal(DATABASE)}) IS NOT NULL BEGIN ALTER DATABASE {quote_identifier(DATABASE)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE {quote_identifier(DATABASE)}; END;",
                    password, timeout=60,
                )
            except PocError:
                cleanup_failed = True
    runner_temp = Path(os.environ.get("RUNNER_TEMP", "/tmp")).absolute()
    for path in (backup_dir, data_dir, download_dir):
        if path is not None:
            if not cleanup_path_is_allowed(path, runner_temp):
                cleanup_failed = True
                continue
            try:
                result = subprocess.run(["sudo", "rm", "-rf", "--", str(path)], capture_output=True, check=False, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                cleanup_failed = True
                continue
            cleanup_failed = cleanup_failed or result.returncode != 0
    if cleanup_failed:
        raise PocError("temporary_data_cleanup_failed")


def main() -> int:
    password: str | None = None
    backup_dir: Path | None = None
    data_dir: Path | None = None
    download_dir: Path | None = None
    try:
        mode = validate_mode(os.environ.get("INPUT_MODE", RESTORE_VALIDATE))
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
        download_dir = runner_temp / f"denmarkbot-poc-download-{run_id}-{suffix}"
        backup_dir = BACKUP_ROOT / f"denmarkbot-poc-backup-{run_id}-{suffix}"
        data_dir = Path("/var/opt/mssql/data") / f"denmarkbot-poc-data-{run_id}-{suffix}"
        run_checked(["sudo", "install", "-d", "-o", os.environ.get("USER", "runner"), "-g", "mssql", "-m", "0700", "--", str(download_dir)], "backup_temp_setup_failed")
        run_checked(["sudo", "install", "-d", "-o", "mssql", "-g", "mssql", "-m", "0750", "--", str(BACKUP_ROOT)], "backup_staging_setup_failed")
        run_checked(["sudo", "install", "-d", "-o", "mssql", "-g", "mssql", "-m", "0750", "--", str(backup_dir)], "backup_staging_setup_failed")
        download_temp_path = download_dir / "oda.bak"
        backup_path = backup_dir / "oda.bak"
        download = download_backup(opener, download_temp_path, content_length)
        print("progress=backup_download_complete")
        backup_size, backup_checksum, download_seconds = validate_download_metadata(download, content_length)
        print(f"backup_bytes={backup_size} sha256={backup_checksum} download_seconds={download_seconds}")
        print("progress=backup_staging_copy")
        run_checked(
            ["sudo", "install", "-o", "mssql", "-g", "mssql", "-m", "0640", "--", str(download_temp_path), str(backup_path)],
            "backup_staging_copy_failed",
            timeout=300,
        )
        download_temp_path.unlink(missing_ok=True)
        restore_parent = Path("/var/opt/mssql/data")
        print("progress=restore_storage_check")
        run_checked(
            ["sudo", "install", "-d", "-o", "mssql", "-g", "mssql", "-m", "0750", str(restore_parent)],
            "restore_data_directory_setup_failed",
        )
        restore_free = check_restore_disk_space(restore_parent, 1)
        validate_backup_readable_by_sql_server(backup_path, backup_size)
        print(f"free_disk_before_restore_bytes={restore_free}")
        print("progress=restore_header_check")
        header_output = sqlcmd(
            f"RESTORE HEADERONLY FROM DISK = {quote_literal(str(backup_path))};",
            password, timeout=600, failure_category="restore_header_request_failed",
        )
        header_info = header_metadata(header_output)
        print("progress=restore_filelist_check")
        file_output = sqlcmd(
            f"RESTORE FILELISTONLY FROM DISK = {quote_literal(str(backup_path))};",
            password, timeout=600, failure_category="restore_filelist_request_failed",
        )
        entries = filelist_metadata(file_output)
        required_restore_space = restore_space_required([int(item["size"]) for item in entries])
        restore_free = check_restore_disk_space(restore_parent, required_restore_space)
        run_checked(["sudo", "install", "-d", "-o", "mssql", "-g", "mssql", "-m", "0750", str(data_dir)], "restore_directory_setup_failed")
        try:
            restore_query = build_restore_query(str(backup_path), str(data_dir), entries)
        except PocError:
            raise
        except (OSError, TypeError, ValueError):
            raise PocError("restore_path_generation_failed") from None
        print("progress=restore_start")
        sqlcmd(restore_query, password, timeout=20 * 60, failure_category="restore_execution_failed")
        print("progress=dbcc_start")
        checkdb = sqlcmd(
            f"DBCC CHECKDB ({quote_identifier(DATABASE)}) WITH ALL_ERRORMSGS;",
            password, timeout=20 * 60, failure_category="dbcc_checkdb_failed",
        )
        if not re.search(r"0 allocation errors and 0 consistency errors", checkdb, re.IGNORECASE):
            raise PocError("dbcc_checkdb_not_clean")
        print("progress=dbcc_pass")
        print("progress=database_size_check")
        restored_size = restored_database_size(password)
        entities = validate_entities(password)
        if entities["missing"] or entities["no_primary_key"]:
            raise PocError("required_entity_validation_failed")
        summary = {
            "download": download,
            "backup_size_bytes": download["file_size_bytes"],
            "header": header_info,
            "restored_database_size_bytes": restored_size,
            "entities": entities,
        }
        if mode == VALIDATE_SQL_PROVIDER:
            print("progress=sql_provider_validation_start")
            provider_validation = validate_sql_provider(password, entities)
            print("SQL provider validation: PASS")
            print(f"entities tested: {provider_validation['entities_tested']}/{len(REQUIRED_ENTITIES)}")
            print(f"get_by_ids checks: {provider_validation['get_by_ids_checks']}")
            print(f"relation checks: {provider_validation['relation_checks']}")
            print(f"normalization failures: {provider_validation['normalization_failures']}")
        if mode == CREATE_MANIFEST_BASELINE:
            compatibility_level = database_compatibility_level(password)
            report_manifest_compatibility(compatibility_level)
            validate_manifest_compatibility_level(compatibility_level)
            print("progress=manifest_schema_discovery")
            schemas: dict[str, dict[str, Any]] = {}
            for entity in REQUIRED_ENTITIES:
                schema_name, object_type = entities["objects"][entity]
                if object_type != "USER_TABLE":
                    raise PocError("manifest_required_entity_not_table")
                schemas[entity] = manifest_schema(password, entity, schema_name)
            manifest_directory = runner_temp / MANIFEST_DIRECTORY_NAME
            if manifest_directory.exists() or manifest_directory.is_symlink():
                raise PocError("manifest_path_exists")
            try:
                manifest_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
            except OSError:
                raise PocError("manifest_directory_setup_failed") from None
            manifest_path = manifest_directory / MANIFEST_FILENAME
            print("progress=manifest_generation")
            try:
                started_entities: set[str] = set()

                def fetch_manifest_page(entity: str, schema_name: str, schema: dict[str, Any], offset: int, limit: int) -> str:
                    if entity not in started_entities:
                        report_manifest_entity(entity)
                        started_entities.add(entity)
                    return manifest_page_json(password, entity, schema_name, schema, offset, limit)

                manifest_info = snapshot_manifest.build_manifest(
                    manifest_path,
                    backup_sha256=str(download["sha256"]),
                    backup_finish_date=str(header_info["backup_finish_date"]),
                    source_sql_major=int(header_info["source_sql_major"]),
                    schemas=schemas,
                    expected_counts={entity: entities["counts"][entity] for entity in REQUIRED_ENTITIES},
                    fetch_page=fetch_manifest_page,
                )
                manifest_info["sha256"] = snapshot_manifest.sha256_file(manifest_path)
            except snapshot_manifest.ManifestError as exc:
                raise PocError(exc.category) from None
            summary["manifest"] = manifest_info
        print("progress=summary_write")
        write_summary(summary)
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
            cleanup(password, backup_dir, data_dir, download_dir)
        except PocError as exc:
            print(f"cleanup=FAIL category={exc.category}", file=sys.stderr)
            return_code = 2
        if password is not None:
            password = None
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
