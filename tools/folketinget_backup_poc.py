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
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = str(Path(__file__).resolve().parents[1])
if REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, REPOSITORY_ROOT)
TOOLS_DIRECTORY = str(Path(__file__).resolve().parent)
if TOOLS_DIRECTORY not in sys.path:
    sys.path.insert(0, TOOLS_DIRECTORY)
import folketinget_snapshot_manifest as snapshot_manifest
import folketinget_snapshot_diff as snapshot_diff
from folketinget_sql_transport import (
    CHUNK_LINE_LENGTH_BUCKETS,
    CHUNK_ROW_SHAPES,
    CHUNK_SEPARATOR_COUNT_BUCKETS,
    MANIFEST_BASE64_CHUNK_SIZE,
    MANIFEST_MAX_BASE64_CHUNKS,
    MANIFEST_SQL_ROW_WIDTH,
    build_chunked_json_query as _build_chunked_json_query,
    classify_chunk_row_structure,
    quote_identifier as _quote_identifier,
)

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
INCREMENTAL_MANIFEST_DIRECTORY_NAME = "denmarkbot-folketinget-manifest-incremental"
MANIFEST_FILENAME = "manifest.sqlite"
RESTORE_VALIDATE = "RESTORE_VALIDATE"
CREATE_MANIFEST_BASELINE = "CREATE_MANIFEST_BASELINE"
VALIDATE_SQL_PROVIDER = "VALIDATE_SQL_PROVIDER"
EXTRACT_SNAPSHOT_CANDIDATES = "EXTRACT_SNAPSHOT_CANDIDATES"
CLASSIFY_SNAPSHOT_CANDIDATES = "CLASSIFY_SNAPSHOT_CANDIDATES"
PREPARE_SNAPSHOT_INGESTION = "PREPARE_SNAPSHOT_INGESTION"
SNAPSHOT_INGESTION_DRY_RUN = "SNAPSHOT_INGESTION_DRY_RUN"
SNAPSHOT_INCREMENTAL_DRY_RUN = "SNAPSHOT_INCREMENTAL_DRY_RUN"
SNAPSHOT_INCREMENTAL_LIVE = "SNAPSHOT_INCREMENTAL_LIVE"
HISTORICAL_SNAPSHOT_BASELINE_DRY_RUN = "HISTORICAL_SNAPSHOT_BASELINE_DRY_RUN"
HISTORICAL_SNAPSHOT_BASELINE_LIVE = "HISTORICAL_SNAPSHOT_BASELINE_LIVE"
MIN_MANIFEST_COMPATIBILITY_LEVEL = 110
SUPPORTED_COMPATIBILITY_LEVELS = frozenset({100, 110, 120, 130, 140, 150, 160, 170})
SQL_PROVIDER_SMOKE_MAX_RELATION_ROWS = 1000


class PocError(Exception):
    """Safe error category; messages must not contain external values."""

    def __init__(
        self,
        category: str,
        *,
        row_shape_category: str | None = None,
        line_length_bucket: str | None = None,
        separator_count_bucket: str | None = None,
    ) -> None:
        self.category = category
        self.row_shape_category = row_shape_category if row_shape_category in CHUNK_ROW_SHAPES else None
        self.line_length_bucket = line_length_bucket if line_length_bucket in CHUNK_LINE_LENGTH_BUCKETS else None
        self.separator_count_bucket = (
            separator_count_bucket if separator_count_bucket in CHUNK_SEPARATOR_COUNT_BUCKETS else None
        )
        super().__init__(category)


def validate_confirmation(value: str) -> None:
    if value != "true":
        raise PocError("manual_confirmation_required")


def validate_snapshot_incremental_live_gate(
    event_name: str, confirm_restore: str, confirm_production: str, automation_enabled: str
) -> None:
    """Fail closed before collection unless live ingestion is explicitly authorized."""
    if confirm_restore != "true":
        raise PocError("manual_confirmation_required")
    if event_name == "schedule":
        if automation_enabled != "true":
            raise PocError("folketinget_automation_disabled")
        return
    if event_name != "workflow_dispatch":
        raise PocError("incremental_live_event_unsupported")
    if confirm_production != "true":
        raise PocError("production_confirmation_required")


def snapshot_incremental_run_id(github_repository: str, github_run_id: str) -> str:
    """Derive a stable idempotency key for retries of the same Actions run."""
    import uuid

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github_repository or ""):
        raise PocError("snapshot_incremental_run_id_invalid")
    if not re.fullmatch(r"[0-9]+", github_run_id or ""):
        raise PocError("snapshot_incremental_run_id_invalid")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://github.com/{github_repository}/actions/runs/{github_run_id}"))


def validate_mode(value: str) -> str:
    if value not in {
        RESTORE_VALIDATE,
        CREATE_MANIFEST_BASELINE,
        VALIDATE_SQL_PROVIDER,
        EXTRACT_SNAPSHOT_CANDIDATES,
        CLASSIFY_SNAPSHOT_CANDIDATES,
        PREPARE_SNAPSHOT_INGESTION,
        SNAPSHOT_INGESTION_DRY_RUN,
        SNAPSHOT_INCREMENTAL_DRY_RUN,
        SNAPSHOT_INCREMENTAL_LIVE,
        HISTORICAL_SNAPSHOT_BASELINE_DRY_RUN,
        HISTORICAL_SNAPSHOT_BASELINE_LIVE,
    }:
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
    try:
        return _quote_identifier(value)
    except (TypeError, ValueError):
        raise PocError("backup_metadata_invalid") from None


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


def build_current_snapshot_manifest(
    password: str, download: dict[str, Any], header_info: dict[str, Any],
    entities: dict[str, Any], runner_temp: Path,
) -> tuple[Path, dict[str, Any]]:
    """Build and validate one private v2 manifest from the restored official backup."""
    compatibility_level = database_compatibility_level(password)
    report_manifest_compatibility(compatibility_level)
    validate_manifest_compatibility_level(compatibility_level)
    schemas: dict[str, dict[str, Any]] = {}
    for entity in REQUIRED_ENTITIES:
        schema_name, object_type = entities["objects"][entity]
        if object_type != "USER_TABLE":
            raise PocError("manifest_required_entity_not_table")
        schemas[entity] = manifest_schema(password, entity, schema_name)
    directory = runner_temp / INCREMENTAL_MANIFEST_DIRECTORY_NAME
    if directory.exists() or directory.is_symlink():
        raise PocError("manifest_path_exists")
    try:
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    except OSError:
        raise PocError("manifest_directory_setup_failed") from None
    path = directory / MANIFEST_FILENAME
    try:
        started_entities: set[str] = set()

        def fetch_page(entity: str, schema_name: str, schema: dict[str, Any], offset: int, limit: int) -> str:
            if entity not in started_entities:
                report_manifest_entity(entity)
                started_entities.add(entity)
            return manifest_page_json(password, entity, schema_name, schema, offset, limit)

        info = snapshot_manifest.build_manifest(
            path,
            backup_sha256=str(download["sha256"]),
            backup_finish_date=str(header_info["backup_finish_date"]),
            source_sql_major=int(header_info["source_sql_major"]),
            schemas=schemas,
            expected_counts={entity: entities["counts"][entity] for entity in REQUIRED_ENTITIES},
            fetch_page=fetch_page,
        )
        metadata = snapshot_diff.validate_manifest(path)
        if metadata["manifest_format_version"] != 2 or metadata["routing_available"] is not True:
            raise PocError("manifest_routing_invalid")
        info["sha256"] = snapshot_manifest.sha256_file(path)
        info["metadata"] = metadata
        return path, info
    except PocError:
        cleanup_incremental_manifest(directory, runner_temp)
        raise
    except snapshot_manifest.ManifestError as error:
        cleanup_incremental_manifest(directory, runner_temp)
        raise PocError(error.category) from None
    except snapshot_diff.DiffError as error:
        cleanup_incremental_manifest(directory, runner_temp)
        raise PocError(error.category) from None
    except Exception:
        cleanup_incremental_manifest(directory, runner_temp)
        raise PocError("manifest_generation_failed") from None


def cleanup_incremental_manifest(directory: Path | None, runner_temp: Path) -> None:
    if directory is None or not (directory.exists() or directory.is_symlink()):
        return
    if (directory.parent != runner_temp or directory.name != INCREMENTAL_MANIFEST_DIRECTORY_NAME
            or directory.is_symlink()):
        raise PocError("manifest_cleanup_failed")
    try:
        shutil.rmtree(directory)
    except OSError:
        raise PocError("manifest_cleanup_failed") from None


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
    """Keep the PoC's safe error vocabulary around the shared query builder."""
    try:
        return _build_chunked_json_query(select_query, variable=variable)
    except (AttributeError, TypeError, ValueError):
        raise PocError("manifest_page_invalid") from None


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

    def fail(stage: str, category: str, *, line: str | None = None) -> None:
        structural = classify_chunk_row_structure(line) if stage == "row" and line is not None else None
        detail_log = ""
        detail_kwargs: dict[str, str] = {}
        if structural is not None:
            row_shape, line_bucket, separator_bucket = structural
            detail_log = (
                f" row_shape={row_shape} line_length_bucket={line_bucket} "
                f"separator_count_bucket={separator_bucket}"
            )
            detail_kwargs = {
                "row_shape_category": row_shape,
                "line_length_bucket": line_bucket,
                "separator_count_bucket": separator_bucket,
            }
        print(
            f"manifest_page_parse_failure entity={entity} offset={offset} "
            f"chunk_count={chunk_count} encoded_chars={encoded_chars} stage={stage} category={category}{detail_log}",
            file=sys.stderr,
        )
        raise PocError(category, **detail_kwargs)

    if not lines or chunk_count > MANIFEST_MAX_BASE64_CHUNKS:
        fail("sequence", "manifest_page_chunk_sequence_invalid")
    chunks: list[str] = []
    for expected_sequence, line in enumerate(lines, start=1):
        parts = line.split("|")
        if len(parts) != 2 or not re.fullmatch(r"[1-9][0-9]{0,4}", parts[0]):
            fail("row", "manifest_page_chunk_row_invalid", line=line)
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
            if not re.fullmatch(r"-?[0-9]+", value):
                raise PocError("sql_provider_sample_unavailable")
            samples[entity] = int(value, 10)
    except PocError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError):
        raise PocError("sql_provider_sample_unavailable") from None
    return samples


def read_sql_provider_relation_samples(password: str, entities: dict[str, Any]) -> list[tuple[str, str, int]]:
    """Choose only relation values with a bounded matching row count."""
    from folketinget_sql_provider import RELATION_FIELDS

    samples: list[tuple[str, str, int]] = []
    try:
        for entity in REQUIRED_ENTITIES:
            schema, object_type = entities["objects"][entity]
            if object_type != "USER_TABLE":
                raise PocError("sql_provider_schema_invalid")
            table = f"{quote_identifier(schema)}.{quote_identifier(entity)}"
            for field in sorted(RELATION_FIELDS[entity]):
                column = quote_identifier(field)
                query = (
                    f"SELECT TOP (1) {column} AS [sample_value], COUNT_BIG(*) AS [match_count] FROM {table} "
                    f"WHERE {column} IS NOT NULL GROUP BY {column} "
                    f"ORDER BY COUNT_BIG(*) ASC, {column} ASC;"
                )
                output = sqlcmd(
                    query, password, database=DATABASE,
                    failure_category="sql_provider_relation_sample_query_failed",
                )
                headers, rows = parse_sqlcmd_table(output)
                if len(rows) > 1:
                    raise PocError("sql_provider_relation_sample_invalid")
                if rows:
                    value_index = field_index(headers, "sample_value")
                    count_index = field_index(headers, "match_count")
                    value_text, count_text = rows[0][value_index], rows[0][count_index]
                    if not re.fullmatch(r"-?[0-9]+", value_text) or not re.fullmatch(r"[0-9]+", count_text):
                        raise PocError("sql_provider_relation_sample_invalid")
                    if int(count_text, 10) > SQL_PROVIDER_SMOKE_MAX_RELATION_ROWS:
                        raise PocError("sql_provider_relation_sample_too_large")
                    samples.append((entity, field, int(value_text, 10)))
    except PocError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError):
        raise PocError("sql_provider_relation_sample_invalid") from None
    return samples


def exercise_sql_provider(
    provider: Any,
    sample_ids: dict[str, int],
    relation_samples: list[tuple[str, str, int]] | None = None,
) -> dict[str, int]:
    """Exercise by-ID and relationship reads; return aggregate-only results."""
    def phase(marker: str, category: str, operation):  # type: ignore[no-untyped-def]
        print(f"progress={marker}")
        try:
            return operation()
        except PocError:
            raise
        except Exception:
            raise PocError(category) from None

    failures = 0

    def preserve_provider_failure(exc: Exception, phase_name: str, entity: str) -> None:
        from folketinget_sql_provider import (
            SAFE_PROVIDER_ERROR_CATEGORIES,
            SAFE_TRANSPORT_CATEGORIES,
        )

        category = getattr(exc, "category", None)
        if not isinstance(category, str) or category not in SAFE_PROVIDER_ERROR_CATEGORIES:
            category = "provider_error_unknown"
        transport_category = getattr(exc, "transport_category", None)
        safe_transport = (
            isinstance(transport_category, str)
            and (
                transport_category in SAFE_TRANSPORT_CATEGORIES
                or re.fullmatch(r"manifest_page_sql_msg_[0-9]{1,6}", transport_category)
            )
        )
        line = f"sql_provider_failure phase={phase_name} entity={entity} category={category}"
        if category == "provider_transport_failed" and safe_transport:
            line += f" transport_category={transport_category}"
        print(line, file=sys.stderr)
        raise PocError(f"sql_provider_{phase_name}_{category}") from None

    def validate_rows(normalize_many, entity: str, rows: Any) -> tuple[dict[str, Any], ...]:
        nonlocal failures
        result = normalize_many(list(rows), entity)
        failures += len(result.diagnostics)
        if not result.complete or len(result.record or ()) != len(rows):
            failures += 1
        return tuple(row for row in rows if isinstance(row, dict))

    def get_by_ids_checks() -> int:
        from collectors.folketinget_models import normalize_many

        if set(sample_ids) != set(REQUIRED_ENTITIES) or any(
            isinstance(value, bool) or not isinstance(value, int) for value in sample_ids.values()
        ):
            raise PocError("sql_provider_sample_validation_failed")
        count = 0
        for entity in REQUIRED_ENTITIES:
            print(f"progress=sql_provider_get_by_ids entity={entity}")
            try:
                rows = tuple(provider.get_by_ids(entity, (sample_ids[entity],)))
            except Exception as exc:
                from folketinget_sql_provider import SQLProviderError

                if isinstance(exc, SQLProviderError):
                    preserve_provider_failure(exc, "get_by_ids", entity)
                raise
            if len(rows) != 1 or rows[0].get("id") != sample_ids[entity]:
                raise PocError("sql_provider_sample_validation_failed")
            normalized = validate_rows(normalize_many, entity, rows)
            if len(normalized) != 1:
                raise PocError("sql_provider_sample_validation_failed")
            count += 1
        return count

    get_checks = phase(
        "sql_provider_get_by_ids", "sql_provider_get_by_ids_failed", get_by_ids_checks
    )

    def relation_checks() -> int:
        from collectors.folketinget_models import normalize_many
        from folketinget_sql_provider import RELATION_FIELDS

        relations = relation_samples or []
        if any(entity not in RELATION_FIELDS or field not in RELATION_FIELDS[entity]
               or isinstance(value, bool) or not isinstance(value, int) for entity, field, value in relations):
            raise PocError("sql_provider_relation_validation_failed")
        for entity, field, value in relations:
            print(f"progress=sql_provider_relation_check entity={entity} field={field}")
            try:
                rows = tuple(provider.find_by_field(entity, field, (value,)))
            except Exception as exc:
                from folketinget_sql_provider import SQLProviderError

                if isinstance(exc, SQLProviderError):
                    preserve_provider_failure(exc, "relation_check", entity)
                raise
            validate_rows(normalize_many, entity, rows)
        if failures:
            raise PocError("sql_provider_normalization_failed")
        return len(relations)

    relation_count = phase(
        "sql_provider_relation_checks", "sql_provider_relation_validation_failed", relation_checks
    )
    return {
        "entities_tested": len(REQUIRED_ENTITIES),
        "get_by_ids_checks": get_checks,
        "relation_checks": relation_count,
        "normalization_failures": failures,
    }


def validate_sql_provider(password: str, entities: dict[str, Any]) -> dict[str, int]:
    def phase(marker: str, category: str, operation):  # type: ignore[no-untyped-def]
        print(f"progress={marker}")
        try:
            return operation()
        except PocError:
            raise
        except Exception:
            raise PocError(category) from None

    try:
        def import_provider():
            from folketinget_sql_provider import (
                PROJECTIONS,
                SQLServerFolketingetProvider,
            )
            return PROJECTIONS, SQLServerFolketingetProvider

        projections, provider_class = phase(
            "sql_provider_import", "sql_provider_import_failed", import_provider
        )
        samples = phase(
            "sql_provider_sample_ids",
            "sql_provider_sample_validation_failed",
            lambda: read_sql_provider_sample_ids(password, entities),
        )
        relation_samples = phase(
            "sql_provider_relation_samples",
            "sql_provider_relation_sample_failed",
            lambda: read_sql_provider_relation_samples(password, entities),
        )

        def check_schema():
            column_types: dict[str, dict[str, str]] = {}
            for entity in REQUIRED_ENTITIES:
                schema_name, object_type = entities["objects"][entity]
                if object_type != "USER_TABLE":
                    raise PocError("sql_provider_schema_invalid")
                schema = manifest_schema(password, entity, schema_name)
                available_types = {column["name"]: column["type_name"] for column in schema["columns"]}
                if set(projections[entity]) - set(available_types):
                    raise PocError("sql_provider_schema_invalid")
                column_types[entity] = {name: available_types[name] for name in projections[entity]}
            return column_types

        column_types = phase(
            "sql_provider_schema_check",
            "sql_provider_schema_validation_failed",
            check_schema,
        )
        provider = phase(
            "sql_provider_construct",
            "sql_provider_construction_failed",
            lambda: provider_class(
                entities["objects"],
                lambda query, entity: run_provider_chunked_json(password, query, entity),
                column_types=column_types,
            ),
        )
        return exercise_sql_provider(provider, samples, relation_samples)
    except PocError:
        raise
    except Exception:
        # Last-resort guard: no provider-validation exception reaches main().
        raise PocError("sql_provider_validation_failed") from None


def validate_candidate_manifest_inputs(
    old_value: Path, new_value: Path, old_run_id: str, new_run_id: str,
    require_newer_snapshot: bool = False,
) -> dict[str, Any]:
    """Validate both downloaded manifests and their declared source chronology."""
    if not re.fullmatch(r"[0-9]+", old_run_id or "") or not re.fullmatch(r"[0-9]+", new_run_id or ""):
        raise PocError("workflow_run_id_invalid")
    old_run_order = old_run_id.lstrip("0") or "0"
    new_run_order = new_run_id.lstrip("0") or "0"
    if old_run_order == new_run_order:
        raise PocError("workflow_run_ids_must_differ")
    try:
        old_path = snapshot_diff._manifest_file(old_value)
        new_path = snapshot_diff._manifest_file(new_value)
        report = snapshot_diff.compare_manifests(old_path, new_path)
    except snapshot_diff.DiffError as error:
        if error.category == "manifest_schema_mismatch":
            raise PocError("snapshot_dry_run_schema_changed" if require_newer_snapshot else "candidate_schema_mismatch") from None
        raise PocError(error.category) from None
    base_snapshot = {
        "backup_sha256": report["old"]["backup_sha256"],
        "backup_finish_date": report["old"]["backup_finish_date"],
        "schema_fingerprint": report["schema_fingerprint"],
    }
    target_snapshot = {
        "backup_sha256": report["new"]["backup_sha256"],
        "backup_finish_date": report["new"]["backup_finish_date"],
        "schema_fingerprint": report["schema_fingerprint"],
    }
    if require_newer_snapshot:
        try:
            from collectors.snapshot_ingestion_client import validate_snapshot_identity
            validate_snapshot_identity(base_snapshot)
            validate_snapshot_identity(target_snapshot)
        except Exception:
            raise PocError("snapshot_dry_run_identity_invalid") from None
    try:
        old_finish = datetime.fromisoformat(report["old"]["backup_finish_date"].replace("Z", "+00:00"))
        new_finish = datetime.fromisoformat(report["new"]["backup_finish_date"].replace("Z", "+00:00"))
        if (old_finish.tzinfo is None) != (new_finish.tzinfo is None):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise PocError("candidate_manifest_finish_invalid") from None
    if old_finish > new_finish:
        raise PocError("snapshot_dry_run_regression" if require_newer_snapshot else "candidate_manifest_order_invalid")
    if require_newer_snapshot and old_finish == new_finish:
        raise PocError("snapshot_dry_run_target_not_newer")
    return {
        "old_path": old_path,
        "new_path": new_path,
        "report": report,
        "old_finish": report["old"]["backup_finish_date"],
        "new_finish": report["new"]["backup_finish_date"],
        "base_snapshot": base_snapshot,
        "target_snapshot": target_snapshot,
    }


def validate_candidate_backup_identity(
    backup_sha256: str, backup_finish_date: str, restored_schema_fingerprint: str,
    candidate_context: dict[str, Any],
) -> None:
    """Require the restored official backup to be exactly the new manifest source."""
    new_metadata = candidate_context["report"]["new"]
    if backup_sha256 != new_metadata["backup_sha256"]:
        raise PocError("candidate_backup_sha_mismatch")
    if backup_finish_date != new_metadata["backup_finish_date"]:
        raise PocError("candidate_backup_finish_mismatch")
    if restored_schema_fingerprint != candidate_context["report"]["schema_fingerprint"]:
        raise PocError("candidate_schema_mismatch")


def validate_historical_manifest_input(value: Path, manifest_run_id: str) -> dict[str, Any]:
    """Validate the one downloaded manifest artifact and retain only source identity metadata."""
    if not re.fullmatch(r"[0-9]+", manifest_run_id or ""):
        raise PocError("workflow_run_id_invalid")
    try:
        if value.is_symlink():
            raise PocError("manifest_artifact_invalid")
        manifest_directory = value.resolve(strict=True)
        expected_directory = f"folketinget-manifest-{manifest_run_id}"
        if manifest_directory.name != expected_directory or not manifest_directory.is_dir() or manifest_directory.is_symlink():
            raise PocError("manifest_artifact_invalid")
        manifest_path = snapshot_diff._manifest_file(manifest_directory)
        metadata = snapshot_diff.validate_manifest(manifest_path)
    except PocError:
        raise
    except snapshot_diff.DiffError as error:
        raise PocError(error.category) from None
    except Exception:
        raise PocError("manifest_artifact_invalid") from None
    return {
        "path": manifest_path,
        "report": {"new": metadata, "schema_fingerprint": metadata["schema_fingerprint"]},
    }


def validate_snapshot_incremental_base_input(value: Path, manifest_run_id: str) -> dict[str, Any]:
    """Require one validated v2 manifest artifact with old-side routing data."""
    if not re.fullmatch(r"[0-9]+", manifest_run_id or ""):
        raise PocError("workflow_run_id_invalid")
    try:
        if value.is_symlink():
            raise PocError("manifest_artifact_invalid")
        directory = value.resolve(strict=True)
        expected_directory = f"folketinget-base-manifest-{manifest_run_id}"
        if directory.name != expected_directory or not directory.is_dir() or directory.is_symlink():
            raise PocError("manifest_artifact_invalid")
        path = snapshot_diff._manifest_file(directory)
        metadata = snapshot_diff.validate_manifest(path)
    except PocError:
        raise
    except snapshot_diff.DiffError as error:
        raise PocError(error.category) from None
    except Exception:
        raise PocError("manifest_artifact_invalid") from None
    if metadata["manifest_format_version"] != 2 or metadata["routing_available"] is not True:
        raise PocError("snapshot_incremental_base_routing_required")
    try:
        from collectors.snapshot_ingestion_client import validate_snapshot_identity
        identity = {
            "backup_sha256": metadata["backup_sha256"],
            "backup_finish_date": metadata["backup_finish_date"],
            "schema_fingerprint": metadata["schema_fingerprint"],
        }
        validate_snapshot_identity(identity)
    except Exception:
        raise PocError("snapshot_incremental_base_identity_invalid") from None
    return {"path": path, "metadata": metadata, "identity": identity, "run_id": manifest_run_id}


def validate_snapshot_incremental_pair(
    base_context: dict[str, Any], target_manifest: Path
) -> dict[str, Any]:
    """Compare exact source identities and return validated aggregate diff data."""
    try:
        target_path = snapshot_diff._manifest_file(target_manifest)
        target_metadata = snapshot_diff.validate_manifest(target_path)
        report = snapshot_diff.compare_manifests(base_context["path"], target_path)
    except snapshot_diff.DiffError as error:
        if error.category == "manifest_schema_mismatch":
            raise PocError("snapshot_incremental_schema_changed") from None
        raise PocError(error.category) from None
    except Exception:
        raise PocError("snapshot_incremental_manifest_invalid") from None
    if target_metadata["manifest_format_version"] != 2 or target_metadata["routing_available"] is not True:
        raise PocError("snapshot_incremental_target_routing_invalid")
    base_identity = dict(base_context["identity"])
    target_identity = {
        "backup_sha256": target_metadata["backup_sha256"],
        "backup_finish_date": target_metadata["backup_finish_date"],
        "schema_fingerprint": target_metadata["schema_fingerprint"],
    }
    try:
        from collectors.snapshot_ingestion_client import validate_snapshot_identity
        validate_snapshot_identity(base_identity)
        validate_snapshot_identity(target_identity)
        base_finish = datetime.fromisoformat(base_identity["backup_finish_date"].replace("Z", "+00:00"))
        target_finish = datetime.fromisoformat(target_identity["backup_finish_date"].replace("Z", "+00:00"))
        if (base_finish.tzinfo is None) != (target_finish.tzinfo is None):
            raise ValueError
    except Exception:
        raise PocError("snapshot_incremental_identity_invalid") from None
    same_snapshot = base_identity == target_identity
    if same_snapshot:
        if any(report["totals"][category] for category in snapshot_diff.CATEGORIES if category != "unchanged"):
            raise PocError("snapshot_incremental_identity_content_conflict")
    elif target_finish <= base_finish:
        raise PocError("snapshot_incremental_target_not_newer")
    return {
        "old_path": base_context["path"],
        "new_path": target_path,
        "report": report,
        "old_finish": base_identity["backup_finish_date"],
        "new_finish": target_identity["backup_finish_date"],
        "base_snapshot": base_identity,
        "target_snapshot": target_identity,
        "same_snapshot": same_snapshot,
    }


def run_snapshot_incremental_dry_run(
    base_context: dict[str, Any], target_manifest: Path, provider: Any, runner_temp: Path
) -> dict[str, Any]:
    """Run extraction, classification and snapshot-client validation without HTTP."""
    context = validate_snapshot_incremental_pair(base_context, target_manifest)
    diff_totals = context["report"]["totals"]
    if context["same_snapshot"]:
        return {
            "status": "PASS", "snapshot_change": "SAME_SNAPSHOT",
            "base_finish": context["old_finish"], "target_finish": context["new_finish"],
            "diff_totals": dict(diff_totals), "affected_case_count": 0, "hydrated_case_count": 0,
            "tombstone_count": 0,
            "DIRECT": 0, "POTENTIAL": 0, "IRRELEVANT": 0, "prepared_record_count": 0,
            "batch_count": 0, "snapshot_client_dry_run": "PASS", "submission_eligible": False,
        }
    result = run_candidate_extraction(
        context["old_path"], context["new_path"], provider, runner_temp,
        context["old_finish"], context["new_finish"],
        snapshot_ingestion_dry_run=True,
        base_snapshot=context["base_snapshot"], target_snapshot=context["target_snapshot"],
    )
    if result.get("diff_totals") != diff_totals:
        raise PocError("snapshot_incremental_diff_contract_mismatch")
    client = result.get("snapshot_ingestion")
    status = "PASS"
    if result.get("complete") is not True or not isinstance(client, dict) or client.get("complete") is not True:
        status = "INCOMPLETE"
    if status == "PASS":
        batch_count = client.get("batch_count")
        if isinstance(batch_count, bool) or not isinstance(batch_count, int) or batch_count > 200:
            raise PocError("snapshot_incremental_batch_limit_exceeded")
    counts = client.get("relevance_counts", {}) if isinstance(client, dict) else {}
    return {
        "status": status, "snapshot_change": "CHANGED",
        "base_finish": context["old_finish"], "target_finish": context["new_finish"],
        "diff_totals": dict(result["diff_totals"]),
        "affected_case_count": result["affected_case_count"],
        "hydrated_case_count": result["hydrated_case_count"],
        "tombstone_count": result["tombstone_count"],
        "DIRECT": counts.get("DIRECT", 0), "POTENTIAL": counts.get("POTENTIAL", 0),
        "IRRELEVANT": counts.get("IRRELEVANT", 0),
        "prepared_record_count": client.get("prepared_record_count", 0) if isinstance(client, dict) else 0,
        "batch_count": client.get("batch_count", 0) if isinstance(client, dict) else 0,
        "snapshot_client_dry_run": "PASS" if status == "PASS" else "FAIL",
        "submission_eligible": client.get("submission_eligible") is True if isinstance(client, dict) else False,
    }


def run_snapshot_incremental_live(
    base_context: dict[str, Any], target_manifest: Path, provider: Any, runner_temp: Path,
    *, endpoint: str, token: str, github_repository: str, github_run_id: str,
) -> dict[str, Any]:
    """Submit a complete newer snapshot diff and require confirmed Worker commit."""
    context = validate_snapshot_incremental_pair(base_context, target_manifest)
    diff_totals = context["report"]["totals"]
    if context["same_snapshot"]:
        return {
            "status": "PASS", "snapshot_change": "SAME_SNAPSHOT",
            "base_finish": context["old_finish"], "target_finish": context["new_finish"],
            "diff_totals": dict(diff_totals), "affected_case_count": 0, "hydrated_case_count": 0,
            "tombstone_count": 0, "DIRECT": 0, "POTENTIAL": 0, "IRRELEVANT": 0,
            "prepared_record_count": 0, "batch_count": 0,
            "snapshot_client_status": "NOT_REQUIRED", "snapshot_advanced": False,
            "promote_manifest": False,
        }
    run_id = snapshot_incremental_run_id(github_repository, github_run_id)
    result = run_candidate_extraction(
        context["old_path"], context["new_path"], provider, runner_temp,
        context["old_finish"], context["new_finish"],
        base_snapshot=context["base_snapshot"], target_snapshot=context["target_snapshot"],
        snapshot_ingestion_live=True, ingestion_endpoint=endpoint, ingestion_token=token,
        ingestion_run_id=run_id,
    )
    if result.get("diff_totals") != diff_totals or result.get("complete") is not True:
        raise PocError("snapshot_incremental_extraction_incomplete")
    client = result.get("snapshot_ingestion_live")
    if not isinstance(client, dict):
        raise PocError("snapshot_incremental_client_result_invalid")
    client_status = client.get("status")
    is_committed = client_status == "COMMITTED"
    if not is_committed:
        raise PocError("snapshot_incremental_client_result_invalid")
    generation = client.get("snapshot_generation")
    if (client.get("snapshot_advanced") is not True
            or client.get("base_snapshot") != context["base_snapshot"]
            or client.get("target_snapshot") != context["target_snapshot"]
            or isinstance(generation, bool) or not isinstance(generation, int) or generation < 2):
        raise PocError("snapshot_incremental_commit_contract_mismatch")
    counts = client.get("relevance_counts")
    records = client.get("records")
    batches = client.get("batches")
    if (not isinstance(counts, dict) or set(counts) != {"DIRECT", "POTENTIAL", "IRRELEVANT"}
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                   for value in counts.values())
            or isinstance(records, bool) or not isinstance(records, int)
            or records != result["candidate_case_count"] + result["tombstone_count"]
            or isinstance(batches, bool) or not isinstance(batches, int) or not 1 <= batches <= 200
            or sum(counts.values()) != result["candidate_case_count"]):
        raise PocError("snapshot_incremental_client_result_invalid")
    return {
        "status": "PASS", "snapshot_change": "CHANGED",
        "base_finish": context["old_finish"], "target_finish": context["new_finish"],
        "diff_totals": dict(diff_totals), "affected_case_count": result["affected_case_count"],
        "hydrated_case_count": result["hydrated_case_count"], "tombstone_count": result["tombstone_count"],
        "DIRECT": counts["DIRECT"], "POTENTIAL": counts["POTENTIAL"], "IRRELEVANT": counts["IRRELEVANT"],
        "prepared_record_count": records, "batch_count": batches,
        "snapshot_client_status": client_status, "snapshot_advanced": is_committed,
        "snapshot_generation": generation,
        "promote_manifest": is_committed,
    }


def write_incremental_promotion_output(enabled: bool) -> None:
    """Set a GitHub output only after the full live path returned COMMITTED."""
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    try:
        with open(output, "a", encoding="utf-8") as target:
            target.write(f"promote_manifest={str(enabled).lower()}\n")
            target.write(f"incremental_outcome={'committed' if enabled else 'no_commit'}\n")
    except OSError:
        raise PocError("snapshot_incremental_output_write_failed") from None


def discover_candidate_provider_schema(
    password: str, entities: dict[str, Any], *, error_category: str = "candidate_schema_mismatch"
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Discover and normalize the six allowlisted SQL provider schemas."""
    try:
        schemas: dict[str, dict[str, Any]] = {}
        for entity in REQUIRED_ENTITIES:
            schema_name, object_type = entities["objects"][entity]
            if object_type != "USER_TABLE":
                raise PocError(error_category)
            schemas[entity] = manifest_schema(password, entity, schema_name)
        restored_fingerprint = snapshot_manifest.schema_fingerprint(schemas)
        provider_schemas = {
            entity: snapshot_manifest.normalize_schema(entity, schemas[entity])
            for entity in REQUIRED_ENTITIES
        }
        return restored_fingerprint, provider_schemas
    except PocError:
        raise
    except Exception:
        raise PocError(error_category) from None


def _prepare_historical_baseline_handoff(
    manifest_context: dict[str, Any], provider: Any, *, progress_callback: Any = None
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    """Run the full proof once and retain selected records only for internal handoff."""
    try:
        import folketinget_historical_baseline as historical_baseline
        from collectors import snapshot_ingestion_client
    except Exception:
        raise PocError("historical_baseline_import_failed") from None
    try:
        summary, selected_records = historical_baseline.run_historical_baseline_with_records(
            manifest_context["path"], provider, progress_callback=progress_callback
        )
    except historical_baseline.HistoricalBaselineError as error:
        raise PocError(error.category) from None
    except Exception:
        raise PocError("historical_baseline_failed") from None
    if not isinstance(summary, dict) or summary.get("status") not in {"PASS", "INCOMPLETE"}:
        raise PocError("historical_baseline_summary_invalid")
    summary = dict(summary)
    client_result: dict[str, Any] = {
        "status": "FAIL", "records": 0, "batches": 0,
        "DIRECT": 0, "POTENTIAL": 0, "submission_eligible": False,
    }
    if summary["status"] == "PASS":
        if not isinstance(selected_records, list) or len(selected_records) != summary.get(
            "selected_historical_ingestion_record_count"
        ):
            raise PocError("historical_handoff_count_mismatch")
        try:
            target_metadata = manifest_context["report"]["new"]
            target_identity = {
                "backup_sha256": target_metadata["backup_sha256"],
                "backup_finish_date": target_metadata["backup_finish_date"],
                "schema_fingerprint": target_metadata["schema_fingerprint"],
            }
            if summary.get("target_backup_finish") != target_identity["backup_finish_date"]:
                raise PocError("historical_handoff_snapshot_mismatch")
            client_result = snapshot_ingestion_client.dry_run_prepared_historical_records(
                selected_records,
                mode="historical",
                state_kind="snapshot",
                dry_run=True,
                base_snapshot=None,
                target_snapshot=target_identity,
            )
        except snapshot_ingestion_client.SnapshotIngestionError:
            summary["diagnostics"] = dict(summary.get("diagnostics", {}))
            summary["diagnostics"]["snapshot_client_dry_run_failed"] = 1
            summary["status"] = "INCOMPLETE"
        except PocError:
            raise
        except Exception:
            raise PocError("historical_handoff_failed") from None
        counts = client_result.get("relevance_counts")
        expected_records = summary.get("selected_historical_ingestion_record_count")
        if (client_result.get("records") != expected_records
                or client_result.get("batches") != summary.get("selected_historical_ingestion_batch_count")
                or not isinstance(counts, dict)
                or counts.get("DIRECT") != summary.get("DIRECT")
                or counts.get("POTENTIAL") != summary.get("POTENTIAL")
                or counts.get("IRRELEVANT") != 0
                or client_result.get("base_snapshot") is not None
                or client_result.get("target_snapshot") != target_identity
                or client_result.get("submission_eligible") is not True):
            summary["diagnostics"] = dict(summary.get("diagnostics", {}))
            summary["diagnostics"]["snapshot_client_contract_mismatch"] = 1
            summary["status"] = "INCOMPLETE"
            client_result = {**client_result, "submission_eligible": False}
        else:
            client_result = {
                "status": "PASS",
                "records": client_result["records"],
                "batches": client_result["batches"],
                "DIRECT": counts["DIRECT"],
                "POTENTIAL": counts["POTENTIAL"],
                "submission_eligible": True,
            }
    summary["snapshot_client_dry_run"] = client_result
    if not isinstance(selected_records, list):
        raise PocError("historical_handoff_count_mismatch")
    target_metadata = manifest_context["report"]["new"]
    target_identity = {
        "backup_sha256": target_metadata["backup_sha256"],
        "backup_finish_date": target_metadata["backup_finish_date"],
        "schema_fingerprint": target_metadata["schema_fingerprint"],
    }
    return summary, selected_records, target_identity


def run_historical_baseline_dry_run(
    manifest_context: dict[str, Any], provider: Any, *, progress_callback: Any = None
) -> dict[str, Any]:
    """Prove the full baseline, then validate its private prepared-record dry-run handoff."""
    summary, _, _ = _prepare_historical_baseline_handoff(
        manifest_context, provider, progress_callback=progress_callback
    )
    return summary


def workflow_historical_run_id(workflow_run_id: str) -> str:
    """Derive a stable UUIDv4-shaped ID from one GitHub Actions run ID."""
    if not isinstance(workflow_run_id, str) or not re.fullmatch(r"[0-9]+", workflow_run_id):
        raise PocError("workflow_run_id_invalid")
    value = bytearray(hashlib.sha256(("DenmarkTgBot:Folketinget:historical:" + workflow_run_id).encode()).digest()[:16])
    value[6] = (value[6] & 0x0F) | 0x40
    value[8] = (value[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(value)))


def run_historical_baseline_live(
    manifest_context: dict[str, Any],
    provider: Any,
    *,
    endpoint: str,
    token: str,
    workflow_run_id: str,
    progress_callback: Any = None,
    opener: Any = None,
) -> dict[str, Any]:
    """Prove the full universe, then submit the exact prepared historical selection."""
    from collectors import snapshot_ingestion_client

    summary, selected_records, target_identity = _prepare_historical_baseline_handoff(
        manifest_context, provider, progress_callback=progress_callback
    )
    run_id = workflow_historical_run_id(workflow_run_id)
    result: dict[str, Any] = {
        "status": "FAIL", "run_id": run_id, "final_ingestion_status": "NOT_STARTED",
        "snapshot_generation": None, "snapshot_advanced": False,
    }
    if historical_baseline_exit_code(summary) != 0:
        result["failure_category"] = "historical_baseline_incomplete"
        summary["snapshot_client_live"] = result
        return summary

    try:
        submit_kwargs: dict[str, Any] = {
            "activation_mode": HISTORICAL_SNAPSHOT_BASELINE_LIVE,
            "mode": "historical",
            "state_kind": "snapshot",
            "base_snapshot": None,
            "target_snapshot": target_identity,
            "endpoint": endpoint,
            "token": token,
            "run_id": run_id,
        }
        if opener is not None:
            submit_kwargs["opener"] = opener
        client_result = snapshot_ingestion_client.submit_prepared_historical_records(
            selected_records, **submit_kwargs
        )
    except snapshot_ingestion_client.SnapshotIngestionError as error:
        # Any error after submission begins can be ambiguous. Preserve the same
        # workflow-derived run ID and never generate a replacement automatically.
        stage = error.stage
        final_status = "NOT_STARTED" if stage in {None, "readiness"} else "UNKNOWN"
        advanced: bool | None = False if final_status == "NOT_STARTED" else None
        result.update({
            "failure_category": error.code,
            "final_ingestion_status": final_status,
            "snapshot_advanced": advanced,
        })
        summary["snapshot_client_live"] = result
        return summary
    except Exception:
        summary["snapshot_client_live"] = {
            **result, "failure_category": "historical_live_submission_failed",
            "final_ingestion_status": "UNKNOWN", "snapshot_advanced": None,
        }
        return summary

    selected_count = summary.get("selected_historical_ingestion_record_count")
    client_counts = client_result.get("relevance_counts")
    processed_count = client_result.get("processed_records")
    processed_contract_ok = (
        (client_result.get("commit_resumed") is True
         and client_result.get("start_idempotent") is True
         and isinstance(processed_count, int) and not isinstance(processed_count, bool)
         and isinstance(selected_count, int) and not isinstance(selected_count, bool)
         and 0 <= processed_count <= selected_count)
        or (client_result.get("commit_resumed") is False and processed_count == selected_count)
    )
    if (client_result.get("status") == "BLOCKED"
            and client_result.get("snapshot_advanced") is False
            and client_result.get("run_id") == run_id):
        result.update({
            "failure_category": "historical_live_run_blocked",
            "final_ingestion_status": "BLOCKED", "snapshot_advanced": False,
        })
        summary["snapshot_client_live"] = result
        return summary
    if client_result.get("status") == "COMMITTED":
        generation = client_result.get("snapshot_generation")
        processed = client_result.get("processed_records")
        if isinstance(generation, int) and not isinstance(generation, bool) and generation >= 1:
            result["snapshot_generation"] = generation
        if client_result.get("snapshot_advanced") is True:
            result["snapshot_advanced"] = True
        if isinstance(processed, int) and not isinstance(processed, bool) and processed >= 0:
            result["final_ingestion_status"] = "COMMITTED"
    if (client_result.get("status") != "COMMITTED"
            or client_result.get("mode") != "historical"
            or client_result.get("state_kind") != "snapshot"
            or client_result.get("run_id") != run_id
            or client_result.get("records") != selected_count
            or client_result.get("accepted_records") != selected_count
            or not processed_contract_ok
            or client_result.get("batches") != summary.get("selected_historical_ingestion_batch_count")
            or not isinstance(client_counts, dict)
            or client_counts.get("DIRECT") != summary.get("DIRECT")
            or client_counts.get("POTENTIAL") != summary.get("POTENTIAL")
            or client_result.get("base_snapshot") is not None
            or client_result.get("target_snapshot") != target_identity
            or client_result.get("snapshot_advanced") is not True
            or isinstance(client_result.get("snapshot_generation"), bool)
            or client_result.get("snapshot_generation") != 1):
        result.update({
            "failure_category": "historical_live_commit_contract_mismatch",
            "final_ingestion_status": "UNKNOWN", "snapshot_advanced": None,
        })
    else:
        result.update({
            "status": "PASS", "final_ingestion_status": "COMMITTED",
            "snapshot_generation": 1, "snapshot_advanced": True,
        })
    summary["snapshot_client_live"] = result
    return summary


def write_historical_baseline_live_summary(summary: dict[str, Any]) -> None:
    """Write safe aggregate fields for the one-time historical live workflow."""
    live = summary.get("snapshot_client_live")
    if not isinstance(live, dict):
        raise PocError("historical_live_summary_invalid")
    status = live.get("status")
    final_status = live.get("final_ingestion_status")
    if (not isinstance(status, str) or status not in {"PASS", "FAIL"}
            or not isinstance(final_status, str)
            or final_status not in {"NOT_STARTED", "UNKNOWN", "COMMITTED", "BLOCKED"}):
        raise PocError("historical_live_summary_invalid")
    run_id = live.get("run_id")
    if not isinstance(run_id, str) or (run_id != "unknown" and not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", run_id
    )):
        raise PocError("historical_live_summary_invalid")
    advanced = live.get("snapshot_advanced")
    if advanced is not None and not isinstance(advanced, bool):
        raise PocError("historical_live_summary_invalid")
    generation = live.get("snapshot_generation")
    if generation is not None and (isinstance(generation, bool) or not isinstance(generation, int) or generation < 1):
        raise PocError("historical_live_summary_invalid")
    finish = summary.get("target_backup_finish", "unknown")
    if finish != "unknown" and (
        not isinstance(finish, str)
        or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9:.+\-Z]{1,24}", finish)
    ):
        raise PocError("historical_live_summary_invalid")
    fields: list[str] = []
    for label, key in (
        ("full Sag count", "manifest_sag_count"),
        ("selected record count", "selected_historical_ingestion_record_count"),
        ("batch count", "selected_historical_ingestion_batch_count"),
        ("DIRECT count", "DIRECT"),
        ("POTENTIAL count", "POTENTIAL"),
    ):
        value = summary.get(key)
        if value is None and summary.get("status") == "INCOMPLETE":
            rendered = "unknown"
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            rendered = str(value)
        else:
            raise PocError("historical_live_summary_invalid")
        fields.append(f"- {label}: {rendered}")
    failure = live.get("failure_category")
    if failure is not None and (not isinstance(failure, str) or not re.fullmatch(r"[a-z0-9_]+", failure)):
        raise PocError("historical_live_summary_invalid")
    lines = [
        f"Historical snapshot baseline live submission: {status}",
        "",
        *fields,
        f"- target backup finish: {finish}",
        f"- run_id: {run_id}",
        f"- final ingestion status: {final_status}",
        f"- snapshot generation: {generation if generation is not None else 'unknown'}",
        f"- snapshot advanced: {str(advanced).lower() if advanced is not None else 'unknown'}",
    ]
    if failure:
        lines.append(f"- error category: {failure}")
    text = "\n".join(lines) + "\n"
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as output:
                output.write(text)
        except OSError:
            raise PocError("historical_live_summary_write_failed") from None
    print(text, end="")


def historical_baseline_live_exit_code(summary: dict[str, Any]) -> int:
    live = summary.get("snapshot_client_live")
    if (summary.get("status") == "PASS" and isinstance(live, dict)
            and live.get("status") == "PASS" and live.get("final_ingestion_status") == "COMMITTED"
            and live.get("snapshot_generation") == 1 and live.get("snapshot_advanced") is True):
        return 0
    return 2


def historical_live_failure_summary(workflow_run_id: str, finish: str | None, category: str) -> dict[str, Any]:
    try:
        run_id = workflow_historical_run_id(workflow_run_id)
    except PocError:
        run_id = "unknown"
    safe_category = category if isinstance(category, str) and re.fullmatch(r"[a-z0-9_]+", category) else "historical_live_failed"
    return {
        "status": "INCOMPLETE",
        "target_backup_finish": finish if isinstance(finish, str) else "unknown",
        "snapshot_client_live": {
            "status": "FAIL", "run_id": run_id, "final_ingestion_status": "NOT_STARTED",
            "snapshot_generation": None, "snapshot_advanced": False,
            "failure_category": safe_category,
        },
    }


def historical_baseline_progress_reporter() -> Any:
    """Return a callback that emits counts only, at 5,000-Sag intervals."""
    next_progress = 5000

    def report(processed: int, total: int) -> None:
        nonlocal next_progress
        if (isinstance(processed, bool) or not isinstance(processed, int) or processed < 0
                or isinstance(total, bool) or not isinstance(total, int) or total < processed):
            raise PocError("historical_progress_invalid")
        if processed >= next_progress:
            print(f"progress=historical_baseline processed={processed} manifest_total={total}")
            next_progress = ((processed // 5000) + 1) * 5000

    return report


def create_candidate_sql_provider(
    password: str, entities: dict[str, Any], schemas: dict[str, dict[str, Any]]
) -> Any:
    """Build the same allowlisted SQL provider used by the validated smoke mode."""
    try:
        from folketinget_sql_provider import PROJECTIONS, SQLServerFolketingetProvider
    except Exception:
        raise PocError("candidate_provider_import_failed") from None
    try:
        column_types: dict[str, dict[str, str]] = {}
        for entity in REQUIRED_ENTITIES:
            schema_name, object_type = entities["objects"][entity]
            if object_type != "USER_TABLE" or schemas[entity]["primary_key"] != ["id"]:
                raise PocError("candidate_provider_schema_invalid")
            available_types = {column["name"]: column["type_name"] for column in schemas[entity]["columns"]}
            if set(PROJECTIONS[entity]) - set(available_types):
                raise PocError("candidate_provider_schema_invalid")
            column_types[entity] = {name: available_types[name] for name in PROJECTIONS[entity]}
    except PocError:
        raise
    except Exception:
        raise PocError("candidate_provider_schema_invalid") from None
    try:
        return SQLServerFolketingetProvider(
            entities["objects"],
            lambda query, entity: run_provider_chunked_json(password, query, entity),
            column_types=column_types,
        )
    except Exception:
        raise PocError("candidate_provider_construction_failed") from None


def _write_private_candidate_bundle(bundle: dict[str, Any], runner_temp: Path) -> Path:
    path: Path | None = None
    try:
        descriptor, filename = tempfile.mkstemp(
            prefix="denmarkbot-candidate-bundle-", suffix=".json", dir=runner_temp
        )
        path = Path(filename)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(bundle, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        if path.stat().st_mode & 0o077:
            raise PocError("candidate_bundle_permissions_invalid")
        return path
    except PocError:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    except Exception:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise PocError("candidate_bundle_write_failed") from None


def cleanup_candidate_bundle(path: Path | None, runner_temp: Path) -> None:
    if path is None:
        return
    if (
        path.parent != runner_temp
        or not path.name.startswith("denmarkbot-candidate-bundle-")
        or path.suffix != ".json"
        or path.is_symlink()
    ):
        raise PocError("candidate_bundle_cleanup_failed")
    try:
        path.unlink(missing_ok=True)
    except OSError:
        raise PocError("candidate_bundle_cleanup_failed") from None


def run_candidate_extraction(
    old_manifest: Path,
    new_manifest: Path,
    provider: Any,
    runner_temp: Path,
    old_finish: str,
    new_finish: str,
    classify_candidates: bool = False,
    prepare_ingestion: bool = False,
    snapshot_ingestion_dry_run: bool = False,
    base_snapshot: dict[str, str] | None = None,
    target_snapshot: dict[str, str] | None = None,
    snapshot_ingestion_live: bool = False,
    ingestion_endpoint: str = "",
    ingestion_token: str = "",
    ingestion_run_id: str | None = None,
) -> dict[str, Any]:
    """Run the established extractor and return only validated aggregate data."""
    bundle_path: Path | None = None
    try:
        try:
            import folketinget_candidate_extraction as candidate_extraction
        except Exception:
            raise PocError("candidate_extractor_import_failed") from None
        try:
            bundle = candidate_extraction.extract_candidates(old_manifest, new_manifest, provider)
        except candidate_extraction.CandidateExtractionError as error:
            raise PocError(error.category) from None
        except Exception:
            raise PocError("candidate_extraction_failed") from None
        bundle_path = _write_private_candidate_bundle(bundle, runner_temp)
        try:
            with bundle_path.open("r", encoding="utf-8") as stored_bundle:
                persisted_bundle = json.load(stored_bundle)
        except Exception:
            raise PocError("candidate_bundle_validation_failed") from None
        if (
            not isinstance(persisted_bundle, dict)
            or persisted_bundle.get("format_version") != 1
            or persisted_bundle.get("source") != "folketinget"
            or not isinstance(persisted_bundle.get("cases"), list)
        ):
            raise PocError("candidate_bundle_validation_failed")
        summary = candidate_extraction.candidate_summary(persisted_bundle)
        diff = summary.get("diff")
        totals = diff.get("totals") if isinstance(diff, dict) else None
        if not isinstance(totals, dict) or set(totals) != set(snapshot_diff.CATEGORIES):
            raise PocError("candidate_summary_invalid")
        clean_totals: dict[str, int] = {}
        for name in snapshot_diff.CATEGORIES:
            value = totals[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PocError("candidate_summary_invalid")
            clean_totals[name] = value
        entity_counts = diff.get("entities")
        if not isinstance(entity_counts, dict) or set(entity_counts) != set(REQUIRED_ENTITIES):
            raise PocError("candidate_summary_invalid")
        clean_entity_counts: dict[str, dict[str, int]] = {}
        for entity in REQUIRED_ENTITIES:
            counts = entity_counts[entity]
            if not isinstance(counts, dict) or set(counts) != set(snapshot_diff.CATEGORIES):
                raise PocError("candidate_summary_invalid")
            clean_counts: dict[str, int] = {}
            for name in snapshot_diff.CATEGORIES:
                value = counts[name]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise PocError("candidate_summary_invalid")
                clean_counts[name] = value
            clean_entity_counts[entity] = clean_counts
        diagnostics = summary.get("diagnostics")
        if not isinstance(diagnostics, list):
            raise PocError("candidate_summary_invalid")
        clean_diagnostics: list[dict[str, Any]] = []
        for item in diagnostics:
            if not isinstance(item, dict) or not re.fullmatch(r"[a-z0-9_]{1,80}", str(item.get("code", ""))):
                raise PocError("candidate_summary_invalid")
            count = item.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise PocError("candidate_summary_invalid")
            clean_diagnostics.append({"code": item["code"], "count": count})
        counts = {}
        for field in ("affected_case_count", "hydrated_case_count"):
            value = summary.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PocError("candidate_summary_invalid")
            counts[field] = value
        candidate_count = persisted_bundle.get("candidate_case_count")
        if isinstance(candidate_count, bool) or not isinstance(candidate_count, int) or candidate_count < 0:
            raise PocError("candidate_summary_invalid")
        tombstones = persisted_bundle.get("tombstones", [])
        if not isinstance(tombstones, list):
            raise PocError("candidate_summary_invalid")
        tombstone_count = len(tombstones)
        result = {
            "complete": summary.get("complete") is True,
            "diff_totals": clean_totals,
            "diff_entities": clean_entity_counts,
            **counts,
            "candidate_case_count": candidate_count,
            "tombstone_count": tombstone_count,
            "diagnostics": clean_diagnostics,
            "old_finish": old_finish,
            "new_finish": new_finish,
        }
        if classify_candidates or prepare_ingestion:
            result["classification"] = _classify_candidate_bundle(persisted_bundle)
        if prepare_ingestion:
            result["ingestion"] = _prepare_snapshot_ingestion(
                persisted_bundle, result["classification"]
            )
        if snapshot_ingestion_dry_run:
            if base_snapshot is None or target_snapshot is None:
                raise PocError("snapshot_dry_run_identity_missing")
            if result["complete"] is not True:
                result["snapshot_ingestion"] = {
                    "complete": False,
                    "candidate_case_count": result["candidate_case_count"],
                    "tombstone_count": result["tombstone_count"],
                    "prepared_record_count": 0,
                    "batch_count": 0,
                    "relevance_counts": {"DIRECT": 0, "POTENTIAL": 0, "IRRELEVANT": 0},
                    "collection_complete": False,
                    "submission_eligible": False,
                    "base_finish": base_snapshot["backup_finish_date"],
                    "target_finish": target_snapshot["backup_finish_date"],
                    "schema_equal": base_snapshot["schema_fingerprint"] == target_snapshot["schema_fingerprint"],
                    "extraction_diagnostics": clean_diagnostics,
                }
            else:
                try:
                    from collectors.snapshot_ingestion_client import (
                        SnapshotIngestionError,
                        ingest_snapshot_candidates,
                    )
                    client_result = ingest_snapshot_candidates(
                        persisted_bundle,
                        mode="incremental",
                        base_snapshot=base_snapshot,
                        target_snapshot=target_snapshot,
                        dry_run=True,
                    )
                except SnapshotIngestionError as error:
                    if re.fullmatch(r"[a-z0-9_]{1,80}", error.code):
                        raise PocError(error.code) from None
                    raise PocError("snapshot_ingestion_client_failed") from None
                except Exception:
                    raise PocError("snapshot_ingestion_client_failed") from None
                counts = client_result.get("relevance_counts")
                prepared_count = client_result.get("records")
                batch_count = client_result.get("batches")
                if (
                    client_result.get("dry_run") is not True
                    or client_result.get("mode") != "incremental"
                    or client_result.get("state_kind") != "snapshot"
                    or client_result.get("collection_complete") is not True
                    or client_result.get("submission_eligible") is not True
                    or isinstance(prepared_count, bool)
                    or not isinstance(prepared_count, int)
                    or prepared_count != result["candidate_case_count"] + result["tombstone_count"]
                    or isinstance(batch_count, bool)
                    or not isinstance(batch_count, int)
                    or batch_count < 1
                    or client_result.get("base_snapshot") != base_snapshot
                    or client_result.get("target_snapshot") != target_snapshot
                    or not isinstance(counts, dict)
                    or set(counts) != {"DIRECT", "POTENTIAL", "IRRELEVANT"}
                    or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values())
                    or sum(counts.values()) != result["candidate_case_count"]
                ):
                    raise PocError("snapshot_ingestion_client_result_invalid")
                result["snapshot_ingestion"] = {
                    "complete": True,
                    "candidate_case_count": result["candidate_case_count"],
                    "tombstone_count": result["tombstone_count"],
                    "prepared_record_count": prepared_count,
                    "batch_count": batch_count,
                    "relevance_counts": counts,
                    "collection_complete": True,
                    "submission_eligible": True,
                    "base_finish": base_snapshot["backup_finish_date"],
                    "target_finish": target_snapshot["backup_finish_date"],
                    "schema_equal": base_snapshot["schema_fingerprint"] == target_snapshot["schema_fingerprint"],
                    "extraction_diagnostics": clean_diagnostics,
                }
        if snapshot_ingestion_live:
            if base_snapshot is None or target_snapshot is None or not ingestion_run_id:
                raise PocError("snapshot_live_contract_missing")
            if result["complete"] is not True or clean_diagnostics:
                raise PocError("snapshot_incremental_extraction_incomplete")
            try:
                from collectors.snapshot_ingestion_client import (
                    SnapshotIngestionError,
                    ingest_snapshot_candidates,
                )
                live_result = ingest_snapshot_candidates(
                    persisted_bundle,
                    mode="incremental",
                    base_snapshot=base_snapshot,
                    target_snapshot=target_snapshot,
                    endpoint=ingestion_endpoint,
                    token=ingestion_token,
                    run_id=ingestion_run_id,
                    dry_run=False,
                )
            except SnapshotIngestionError as error:
                if re.fullmatch(r"[a-z0-9_]{1,80}", error.code):
                    raise PocError(error.code) from None
                raise PocError("snapshot_incremental_ingestion_failed") from None
            except Exception:
                raise PocError("snapshot_incremental_ingestion_failed") from None
            counts = live_result.get("relevance_counts")
            records_count = live_result.get("records")
            batches_count = live_result.get("batches")
            generation = live_result.get("snapshot_generation")
            if (live_result.get("dry_run") is not False
                    or live_result.get("mode") != "incremental"
                    or live_result.get("state_kind") != "snapshot"
                    or live_result.get("collection_complete") is not True
                    or live_result.get("submission_eligible") is not True
                    or live_result.get("status") != "COMMITTED"
                    or live_result.get("snapshot_advanced") is not True
                    or live_result.get("run_id") != ingestion_run_id
                    or live_result.get("base_snapshot") != base_snapshot
                    or live_result.get("target_snapshot") != target_snapshot
                    or isinstance(records_count, bool) or not isinstance(records_count, int)
                    or records_count != result["candidate_case_count"] + tombstone_count
                    or isinstance(batches_count, bool) or not isinstance(batches_count, int)
                    or not 1 <= batches_count <= 200
                    or isinstance(generation, bool) or not isinstance(generation, int) or generation < 2
                    or not isinstance(counts, dict) or set(counts) != {"DIRECT", "POTENTIAL", "IRRELEVANT"}
                    or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values())
                    or sum(counts.values()) != result["candidate_case_count"]):
                raise PocError("snapshot_incremental_commit_contract_mismatch")
            result["snapshot_ingestion_live"] = {
                "status": "COMMITTED", "records": records_count, "batches": batches_count,
                "relevance_counts": counts, "base_snapshot": base_snapshot,
                "target_snapshot": target_snapshot, "snapshot_advanced": True,
                "snapshot_generation": generation,
            }
        return result
    finally:
        cleanup_candidate_bundle(bundle_path, runner_temp)


def _classify_candidate_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Classify candidate cases in memory and return aggregate-only results."""
    try:
        from collectors import relevance
    except Exception:
        raise PocError("candidate_classifier_import_failed") from None
    cases = bundle.get("cases")
    candidate_count = bundle.get("candidate_case_count")
    if (
        not isinstance(cases, list)
        or isinstance(candidate_count, bool)
        or not isinstance(candidate_count, int)
        or candidate_count != len(cases)
    ):
        raise PocError("candidate_classification_count_mismatch")

    category_counts = {
        relevance.DIRECT: 0,
        relevance.POTENTIAL: 0,
        relevance.IRRELEVANT: 0,
    }
    classified_count = 0
    notification_blocked_count = 0
    diagnostics_count = 0
    ruleset_version = relevance.RULESET_VERSION
    for case in cases:
        if (
            not isinstance(case, dict)
            or case.get("source") != "folketinget"
            or case.get("historical") is not False
            or case.get("notify_eligible") is not None
        ):
            raise PocError("candidate_classification_case_invalid")
        try:
            classification = relevance.classify(case)
        except Exception:
            raise PocError("candidate_classification_failed") from None
        category = getattr(classification, "relevance", None)
        result_ruleset = getattr(classification, "ruleset_version", None)
        diagnostics = getattr(classification, "diagnostics", None)
        notification_blocked = getattr(classification, "notification_blocked", None)
        if (
            category not in category_counts
            or result_ruleset != ruleset_version
            or not isinstance(diagnostics, (tuple, list))
            or not isinstance(notification_blocked, bool)
        ):
            raise PocError("candidate_classification_result_invalid")
        category_counts[category] += 1
        classified_count += 1
        diagnostics_count += len(diagnostics)
        notification_blocked_count += int(notification_blocked)

    if classified_count != candidate_count:
        raise PocError("candidate_classification_count_mismatch")
    extraction_complete = bundle.get("complete") is True
    extraction_diagnostics = bundle.get("diagnostics")
    if not isinstance(extraction_diagnostics, list):
        raise PocError("candidate_bundle_validation_failed")
    clean_extraction_diagnostics: list[dict[str, Any]] = []
    for diagnostic in extraction_diagnostics:
        if (
            not isinstance(diagnostic, dict)
            or not re.fullmatch(r"[a-z0-9_]{1,80}", str(diagnostic.get("code", "")))
            or isinstance(diagnostic.get("count"), bool)
            or not isinstance(diagnostic.get("count"), int)
            or diagnostic["count"] < 0
        ):
            raise PocError("candidate_bundle_validation_failed")
        clean_extraction_diagnostics.append({"code": diagnostic["code"], "count": diagnostic["count"]})
    return {
        "candidate_case_count": candidate_count,
        "classified_case_count": classified_count,
        "relevance_counts": category_counts,
        "notification_blocked_count": notification_blocked_count,
        "classification_diagnostics_count": diagnostics_count,
        "ruleset_version": ruleset_version,
        "extraction_complete": extraction_complete,
        "complete": extraction_complete,
        "extraction_diagnostics": clean_extraction_diagnostics,
    }


def write_candidate_classification_summary(summary: dict[str, Any]) -> None:
    classification = summary["classification"]
    state = "PASS" if classification["complete"] else "INCOMPLETE"
    relevance_counts = classification["relevance_counts"]
    lines = [
        f"Snapshot relevance dry-run: {state}",
        "",
        f"- candidate case count: {classification['candidate_case_count']}",
        f"- classified case count: {classification['classified_case_count']}",
        f"- DIRECT: {relevance_counts['DIRECT']}",
        f"- POTENTIAL: {relevance_counts['POTENTIAL']}",
        f"- IRRELEVANT: {relevance_counts['IRRELEVANT']}",
        f"- notification_blocked count: {classification['notification_blocked_count']}",
        f"- classification diagnostics count: {classification['classification_diagnostics_count']}",
        f"- ruleset_version: {classification['ruleset_version']}",
        f"- extraction complete: {str(classification['extraction_complete']).lower()}",
        "- extraction diagnostics:",
    ]
    if classification["extraction_diagnostics"]:
        lines.extend(
            f"  - {item['code']}: {item['count']}"
            for item in classification["extraction_diagnostics"]
        )
    else:
        lines.append("  - none")
    lines.extend([
        f"- old backup finish: {summary['old_finish']}",
        f"- new backup finish: {summary['new_finish']}",
        "",
    ])
    text = "\n".join(lines)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as output:
                output.write(text)
        except OSError:
            raise PocError("candidate_classification_summary_write_failed") from None
    print(text, end="")


def _prepare_snapshot_ingestion(
    bundle: dict[str, Any], classification: dict[str, Any]
) -> dict[str, Any]:
    """Exercise record preparation and batching only; never use OData checkpoints or upload()."""
    try:
        from collectors import ingestion_client
        from collectors.relevance import DIRECT, IRRELEVANT, POTENTIAL
    except Exception:
        raise PocError("snapshot_ingestion_import_failed") from None

    cases = bundle.get("cases")
    if not isinstance(cases, list):
        raise PocError("snapshot_ingestion_bundle_invalid")
    for case in cases:
        if (
            not isinstance(case, dict)
            or case.get("source") != "folketinget"
            or case.get("historical") is not False
            or case.get("notify_eligible") is not None
            or not isinstance(case.get("complete"), bool)
            or any(not isinstance(case.get(name), list) for name in ("documents", "stages", "votes"))
        ):
            raise PocError("snapshot_ingestion_case_invalid")
    try:
        records = ingestion_client.prepare_records(
            {"cases": cases}, source="folketinget", mode="incremental"
        )
    except ingestion_client.IngestionError:
        raise PocError("snapshot_ingestion_preparation_failed") from None
    except Exception:
        raise PocError("snapshot_ingestion_preparation_failed") from None
    if not isinstance(records, list):
        raise PocError("snapshot_ingestion_preparation_failed")

    relevance_counts = {DIRECT: 0, POTENTIAL: 0, IRRELEVANT: 0}
    notification_blocked_count = 0
    complete_candidate_count = 0
    incomplete_candidate_count = 0
    for record in records:
        if not isinstance(record, dict):
            raise PocError("snapshot_ingestion_contract_invalid")
        source = record.get("source")
        historical = record.get("historical")
        complete = record.get("complete")
        notification_blocked = record.get("notification_blocked")
        relevance = record.get("relevance")
        ruleset_version = record.get("ruleset_version")
        url = record.get("url")
        normalized_snapshot = record.get("normalized_snapshot")
        if (
            source != "folketinget"
            or historical is not False
            or not isinstance(complete, bool)
            or not isinstance(notification_blocked, bool)
            or relevance not in relevance_counts
            or ruleset_version != ingestion_client.RULESET_VERSION
            or not _is_official_oda_case_url(url)
            or not isinstance(normalized_snapshot, dict)
            or any(not isinstance(normalized_snapshot.get(name), list) for name in ("documents", "stages", "votes"))
        ):
            raise PocError("snapshot_ingestion_contract_invalid")
        relevance_counts[relevance] += 1
        notification_blocked_count += int(notification_blocked)
        if complete:
            complete_candidate_count += 1
        else:
            incomplete_candidate_count += 1

    if len(records) != classification.get("classified_case_count"):
        raise PocError("snapshot_ingestion_record_count_mismatch")
    if (
        relevance_counts != classification.get("relevance_counts")
        or notification_blocked_count != classification.get("notification_blocked_count")
    ):
        raise PocError("snapshot_ingestion_classification_mismatch")

    try:
        batches = ingestion_client.chunks(records)
    except ingestion_client.IngestionError:
        raise PocError("snapshot_ingestion_batching_failed") from None
    except Exception:
        raise PocError("snapshot_ingestion_batching_failed") from None
    if not isinstance(batches, list) or not batches or any(not isinstance(batch, list) for batch in batches):
        raise PocError("snapshot_ingestion_batching_invalid")
    if [record for batch in batches for record in batch] != records:
        raise PocError("snapshot_ingestion_batching_invalid")
    largest_batch_records = max(len(batch) for batch in batches)
    largest_batch_bytes = max(
        len(json.dumps({"records": batch}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        for batch in batches
    )
    extraction_complete = bundle.get("complete") is True
    # Deliberately never mark a snapshot-native batch eligible for submission here.
    return {
        "prepared_record_count": len(records),
        "batch_count": len(batches),
        "largest_batch_record_count": largest_batch_records,
        "largest_batch_encoded_bytes": largest_batch_bytes,
        "relevance_counts": relevance_counts,
        "notification_blocked_count": notification_blocked_count,
        "complete_candidate_count": complete_candidate_count,
        "incomplete_candidate_count": incomplete_candidate_count,
        "extraction_complete": extraction_complete,
        "complete": extraction_complete,
        "ruleset_version": ingestion_client.RULESET_VERSION,
        "extraction_diagnostics": classification["extraction_diagnostics"],
        "submission_eligible": False,
        "empty_batch_representation": "one empty batch" if not records and batches == [[]] else None,
    }


def _is_official_oda_case_url(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "oda.ft.dk"
            and parsed.username is None
            and parsed.password is None
            and re.fullmatch(r"/api/Sag\(\d{1,20}\)", parsed.path) is not None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def write_snapshot_ingestion_summary(summary: dict[str, Any]) -> None:
    ingestion = summary["ingestion"]
    state = "PASS" if ingestion["complete"] else "INCOMPLETE"
    relevance_counts = ingestion["relevance_counts"]
    lines = [
        f"Snapshot ingestion contract dry-run: {state}",
        "",
        f"- prepared records: {ingestion['prepared_record_count']}",
        f"- batch count: {ingestion['batch_count']}",
        f"- largest batch records: {ingestion['largest_batch_record_count']}",
        f"- largest encoded batch bytes: {ingestion['largest_batch_encoded_bytes']}",
        f"- DIRECT: {relevance_counts['DIRECT']}",
        f"- POTENTIAL: {relevance_counts['POTENTIAL']}",
        f"- IRRELEVANT: {relevance_counts['IRRELEVANT']}",
        f"- notification_blocked count: {ingestion['notification_blocked_count']}",
        f"- complete candidates: {ingestion['complete_candidate_count']}",
        f"- incomplete candidates: {ingestion['incomplete_candidate_count']}",
        f"- extraction complete: {str(ingestion['extraction_complete']).lower()}",
        f"- ruleset_version: {ingestion['ruleset_version']}",
        f"- submission eligible: {str(ingestion['submission_eligible']).lower()}",
    ]
    if ingestion["empty_batch_representation"]:
        lines.append(f"- empty input behavior: {ingestion['empty_batch_representation']}")
    lines.append("- extraction diagnostics:")
    if ingestion["extraction_diagnostics"]:
        lines.extend(
            f"  - {item['code']}: {item['count']}"
            for item in ingestion["extraction_diagnostics"]
        )
    else:
        lines.append("  - none")
    lines.append("")
    text = "\n".join(lines)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as output:
                output.write(text)
        except OSError:
            raise PocError("snapshot_ingestion_summary_write_failed") from None
    print(text, end="")


def write_snapshot_ingestion_client_summary(summary: dict[str, Any]) -> None:
    """Emit aggregate-only output from the client dry-run integration."""
    ingestion = summary["snapshot_ingestion"]
    state = "PASS" if ingestion["complete"] and summary["complete"] else "INCOMPLETE"
    counts = ingestion["relevance_counts"]
    lines = [
        f"Snapshot ingestion client dry-run: {state}",
        "",
        f"- candidate cases: {ingestion['candidate_case_count']}",
        f"- source deletion operations: {ingestion.get('tombstone_count', 0)}",
        f"- prepared records: {ingestion['prepared_record_count']}",
        f"- batches: {ingestion['batch_count']}",
        f"- DIRECT: {counts['DIRECT']}",
        f"- POTENTIAL: {counts['POTENTIAL']}",
        f"- IRRELEVANT: {counts['IRRELEVANT']}",
        f"- collection complete: {str(ingestion['collection_complete']).lower()}",
        f"- submission eligible: {str(ingestion['submission_eligible']).lower()}",
        f"- base backup finish: {ingestion['base_finish']}",
        f"- target backup finish: {ingestion['target_finish']}",
        f"- base/target schema equality: {str(ingestion['schema_equal']).lower()}",
        "- extraction diagnostics:",
    ]
    if ingestion["extraction_diagnostics"]:
        lines.extend(f"  - {item['code']}: {item['count']}" for item in ingestion["extraction_diagnostics"])
    else:
        lines.append("  - none")
    text = "\n".join(lines) + "\n"
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as output:
                output.write(text)
        except OSError:
            raise PocError("snapshot_ingestion_summary_write_failed") from None
    print(text, end="")


def write_snapshot_incremental_summary(summary: dict[str, Any]) -> None:
    """Emit only aggregate snapshot-diff and dry-run contract fields."""
    diff = summary["diff_totals"]
    lines = [
        f"Snapshot incremental dry-run: {summary['status']}",
        "",
        f"- base backup finish: {summary['base_finish']}",
        f"- target backup finish: {summary['target_finish']}",
        f"- snapshot comparison: {summary['snapshot_change']}",
        f"- added: {diff['added']}",
        f"- removed: {diff['removed']}",
        f"- changed: {diff['changed']}",
        f"- technical_only: {diff['technical_only']}",
        f"- affected Sag count: {summary['affected_case_count']}",
        f"- source deletion operations: {summary.get('tombstone_count', 0)}",
        f"- hydrated Sag count: {summary['hydrated_case_count']}",
        f"- DIRECT: {summary['DIRECT']}",
        f"- POTENTIAL: {summary['POTENTIAL']}",
        f"- IRRELEVANT: {summary['IRRELEVANT']}",
        f"- prepared record count: {summary['prepared_record_count']}",
        f"- batch count: {summary['batch_count']}",
        f"- snapshot client dry-run: {summary['snapshot_client_dry_run']}",
        f"- submission eligible: {str(summary['submission_eligible']).lower()}",
    ]
    text = "\n".join(lines) + "\n"
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as output:
                output.write(text)
        except OSError:
            raise PocError("snapshot_incremental_summary_write_failed") from None
    print(text, end="")


def write_snapshot_incremental_live_summary(summary: dict[str, Any]) -> None:
    """Emit aggregate-only production outcome without record or run identifiers."""
    diff = summary["diff_totals"]
    lines = [
        f"Folketinget snapshot incremental production: {summary['status']}", "",
        f"- base backup finish: {summary['base_finish']}",
        f"- target backup finish: {summary['target_finish']}",
        f"- snapshot comparison: {summary['snapshot_change']}",
        f"- added: {diff['added']}", f"- removed: {diff['removed']}",
        f"- changed: {diff['changed']}", f"- technical_only: {diff['technical_only']}",
        f"- affected Sag count: {summary['affected_case_count']}",
        f"- hydrated Sag count: {summary['hydrated_case_count']}",
        f"- DIRECT: {summary['DIRECT']}", f"- POTENTIAL: {summary['POTENTIAL']}",
        f"- IRRELEVANT: {summary['IRRELEVANT']}",
        f"- prepared record count: {summary['prepared_record_count']}",
        f"- batch count: {summary['batch_count']}",
        f"- Worker ingestion status: {summary['snapshot_client_status']}",
        f"- snapshot advanced: {str(summary['snapshot_advanced']).lower()}",
    ]
    text = "\n".join(lines) + "\n"
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as output:
                output.write(text)
        except OSError:
            raise PocError("snapshot_incremental_summary_write_failed") from None
    print(text, end="")


def write_historical_baseline_summary(summary: dict[str, Any]) -> None:
    """Write only the documented aggregate fields for historical baseline dry-run."""
    import folketinget_candidate_extraction as candidate_extraction

    expected_integer_fields = (
        "manifest_sag_count", "hydrated_case_count", "prepared_record_count", "DIRECT", "POTENTIAL",
        "IRRELEVANT", "notification_blocked_count", "selected_historical_ingestion_record_count",
        "selected_historical_ingestion_batch_count", "selected_largest_batch_records",
        "selected_largest_batch_bytes", "incomplete_case_count", "provider_adaptive_splits",
        "provider_adaptive_split_successes", "provider_max_split_depth",
        "provider_terminal_singleton_failures",
    )
    if summary.get("status") not in {"PASS", "INCOMPLETE"}:
        raise PocError("historical_baseline_summary_invalid")
    if (not isinstance(summary.get("full_universe_complete"), bool)
            or not isinstance(summary.get("selected_payload_fits_worker_limit"), bool)):
        raise PocError("historical_baseline_summary_invalid")
    if summary["status"] == "PASS" and (
        summary["full_universe_complete"] is not True
        or summary["selected_payload_fits_worker_limit"] is not True
    ):
        raise PocError("historical_baseline_summary_invalid")
    client = summary.get("snapshot_client_dry_run")
    if not isinstance(client, dict) or client.get("status") not in {"PASS", "FAIL"}:
        raise PocError("historical_baseline_summary_invalid")
    client_clean: dict[str, int] = {}
    for field in ("records", "batches", "DIRECT", "POTENTIAL"):
        value = client.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PocError("historical_baseline_summary_invalid")
        client_clean[field] = value
    if not isinstance(client.get("submission_eligible"), bool):
        raise PocError("historical_baseline_summary_invalid")
    if summary["status"] == "PASS" and (
        client.get("status") != "PASS" or client.get("submission_eligible") is not True
    ):
        raise PocError("historical_baseline_summary_invalid")
    clean: dict[str, int] = {}
    for field in expected_integer_fields:
        # Optional for compatibility with pre-adaptive fixture/provider summaries.
        value = summary.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PocError("historical_baseline_summary_invalid")
        clean[field] = value
    finish = summary.get("target_backup_finish")
    ruleset_version = summary.get("ruleset_version")
    diagnostics = summary.get("diagnostics")
    if (not isinstance(finish, str)
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9:.+\-Z]{1,24}", finish)
            or not isinstance(ruleset_version, str)
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", ruleset_version)):
        raise PocError("historical_baseline_summary_invalid")
    if not isinstance(diagnostics, dict):
        raise PocError("historical_baseline_summary_invalid")
    diagnostic_lines = []
    for code, count in sorted(diagnostics.items()):
        if not candidate_extraction.is_safe_diagnostic_code(code):
            raise PocError("historical_baseline_summary_invalid")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise PocError("historical_baseline_summary_invalid")
        diagnostic_lines.append(f"  - {code}: {count}")
    terminal_shapes = summary.get("provider_terminal_transport_row_shapes", {})
    terminal_lengths = summary.get("provider_terminal_transport_line_length_buckets", {})
    for values_map, allowed in (
        (terminal_shapes, CHUNK_ROW_SHAPES),
        (terminal_lengths, CHUNK_LINE_LENGTH_BUCKETS),
    ):
        if not isinstance(values_map, dict) or any(
            key not in allowed or isinstance(count, bool) or not isinstance(count, int) or count < 1
            for key, count in values_map.items()
        ):
            raise PocError("historical_baseline_summary_invalid")
        if sum(values_map.values()) > clean["provider_terminal_singleton_failures"]:
            raise PocError("historical_baseline_summary_invalid")
    row_shape_lines = [
        f"  - {shape}: {terminal_shapes.get(shape, 0)}" for shape in sorted(CHUNK_ROW_SHAPES)
    ]
    line_length_order = ("0", "1-16", "17-64", "65-256", ">256")
    line_length_lines = [
        f"  - {bucket}: {terminal_lengths.get(bucket, 0)}" for bucket in line_length_order
    ]
    lines = [
        f"Historical snapshot baseline dry-run: {summary['status']}",
        "",
        f"- manifest Sag count: {clean['manifest_sag_count']}",
        f"- hydrated case count: {clean['hydrated_case_count']}",
        f"- prepared record count: {clean['prepared_record_count']}",
        f"- DIRECT: {clean['DIRECT']}",
        f"- POTENTIAL: {clean['POTENTIAL']}",
        f"- IRRELEVANT: {clean['IRRELEVANT']}",
        f"- notification_blocked count: {clean['notification_blocked_count']}",
        f"- full-universe completeness proof: {str(summary['full_universe_complete']).lower()}",
        f"- selected historical ingestion records: {clean['selected_historical_ingestion_record_count']}",
        f"- selected historical ingestion batches: {clean['selected_historical_ingestion_batch_count']}",
        f"- selected largest batch records: {clean['selected_largest_batch_records']}",
        f"- selected largest batch bytes: {clean['selected_largest_batch_bytes']}",
        f"- selected payload fits Worker 200-batch limit: {str(summary['selected_payload_fits_worker_limit']).lower()}",
        f"- snapshot client dry-run: {client['status']}",
        f"- snapshot client records: {client_clean['records']}",
        f"- snapshot client batches: {client_clean['batches']}",
        f"- snapshot client DIRECT: {client_clean['DIRECT']}",
        f"- snapshot client POTENTIAL: {client_clean['POTENTIAL']}",
        f"- snapshot client submission eligible: {str(client['submission_eligible']).lower()}",
        f"- incomplete case count: {clean['incomplete_case_count']}",
        f"- provider adaptive splits: {clean['provider_adaptive_splits']}",
        f"- provider adaptive split successes: {clean['provider_adaptive_split_successes']}",
        f"- provider max split depth: {clean['provider_max_split_depth']}",
        f"- provider terminal singleton failures: {clean['provider_terminal_singleton_failures']}",
        "- terminal transport row shapes:",
        *row_shape_lines,
        "- terminal transport line length buckets:",
        *line_length_lines,
        "- diagnostics:",
        *(diagnostic_lines or ["  - none"]),
        f"- target backup finish: {finish}",
        f"- ruleset_version: {ruleset_version}",
        "",
    ]
    text = "\n".join(lines)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as output:
                output.write(text)
        except OSError:
            raise PocError("historical_baseline_summary_write_failed") from None
    print(text, end="")


def historical_baseline_exit_code(summary: dict[str, Any]) -> int:
    client = summary.get("snapshot_client_dry_run")
    if (summary.get("status") == "PASS" and isinstance(client, dict)
            and client.get("status") == "PASS" and client.get("submission_eligible") is True):
        return 0
    if summary.get("status") == "INCOMPLETE" or (isinstance(client, dict) and client.get("status") == "FAIL"):
        return 2
    raise PocError("historical_baseline_summary_invalid")


def write_candidate_extraction_summary(summary: dict[str, Any]) -> None:
    totals = summary["diff_totals"]
    state = "PASS" if summary["complete"] else "INCOMPLETE"
    lines = [
        f"Snapshot candidate extraction: {state}",
        "",
        "Diff totals:",
        *[f"- {name}: {totals[name]}" for name in snapshot_diff.CATEGORIES],
        "- per entity:",
        *[
            f"  - {entity}: " + ", ".join(
                f"{category}={summary['diff_entities'][entity][category]}"
                for category in snapshot_diff.CATEGORIES
            )
            for entity in REQUIRED_ENTITIES
        ],
        f"- affected case count: {summary['affected_case_count']}",
        f"- hydrated case count: {summary['hydrated_case_count']}",
        f"- candidate case count: {summary['candidate_case_count']}",
        "- diagnostics:",
    ]
    if summary["diagnostics"]:
        lines.extend(f"  - {item['code']}: {item['count']}" for item in summary["diagnostics"])
    else:
        lines.append("  - none")
    lines.extend([
        f"- complete: {str(summary['complete']).lower()}",
        f"- old backup finish: {summary['old_finish']}",
        f"- new backup finish: {summary['new_finish']}",
        "",
    ])
    text = "\n".join(lines)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as output:
            output.write(text)
    print(text, end="")


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
    incremental_manifest_dir: Path | None = None
    runner_temp: Path | None = None
    candidate_context: dict[str, Any] | None = None
    historical_context: dict[str, Any] | None = None
    incremental_base_context: dict[str, Any] | None = None
    mode: str | None = None
    historical_live_summary_written = False
    incremental_manifest_promotable = False
    try:
        mode = validate_mode(os.environ.get("INPUT_MODE", RESTORE_VALIDATE))
        validate_confirmation(os.environ.get("INPUT_CONFIRM_RESTORE", ""))
        candidate_modes = {
            EXTRACT_SNAPSHOT_CANDIDATES,
            CLASSIFY_SNAPSHOT_CANDIDATES,
            PREPARE_SNAPSHOT_INGESTION,
            SNAPSHOT_INGESTION_DRY_RUN,
        }
        incremental_mode = mode in {SNAPSHOT_INCREMENTAL_DRY_RUN, SNAPSHOT_INCREMENTAL_LIVE}
        incremental_live_mode = mode == SNAPSHOT_INCREMENTAL_LIVE
        if incremental_live_mode:
            validate_snapshot_incremental_live_gate(
                os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"),
                os.environ.get("INPUT_CONFIRM_RESTORE", ""),
                os.environ.get("INPUT_CONFIRM_PRODUCTION", ""),
                os.environ.get("FOLKETINGET_AUTOMATION_ENABLED", ""),
            )
            if not os.environ.get("INGEST_API_URL") or not os.environ.get("INGEST_API_TOKEN"):
                raise PocError("ingestion_configuration_missing")
        historical_mode = mode in {
            HISTORICAL_SNAPSHOT_BASELINE_DRY_RUN,
            HISTORICAL_SNAPSHOT_BASELINE_LIVE,
        }
        if mode in candidate_modes:
            candidate_context = validate_candidate_manifest_inputs(
                Path(os.environ.get("OLD_MANIFEST_PATH", "")),
                Path(os.environ.get("NEW_MANIFEST_PATH", "")),
                os.environ.get("INPUT_OLD_RUN_ID", ""),
                os.environ.get("INPUT_NEW_RUN_ID", ""),
                require_newer_snapshot=mode == SNAPSHOT_INGESTION_DRY_RUN,
            )
        elif historical_mode:
            historical_context = validate_historical_manifest_input(
                Path(os.environ.get("HISTORICAL_MANIFEST_PATH", "")),
                os.environ.get("INPUT_MANIFEST_RUN_ID", ""),
            )
        elif incremental_mode:
            incremental_base_context = validate_snapshot_incremental_base_input(
                Path(os.environ.get("BASE_MANIFEST_PATH", "")),
                os.environ.get("INPUT_BASE_MANIFEST_RUN_ID", ""),
            )
        username = os.environ.get("ODA_BACKUP_USERNAME", "")
        credential = os.environ.get("ODA_BACKUP_PASSWORD", "")
        validate_credentials(username, credential)
        release = platform.freedesktop_os_release().get("VERSION_ID", "")
        require_x64_ubuntu24(platform.machine(), release)
        runner_temp = Path(os.environ.get("RUNNER_TEMP", "/tmp")).resolve()
        initial_free = free_bytes(runner_temp)
        quiet_modes = candidate_modes | {
            HISTORICAL_SNAPSHOT_BASELINE_DRY_RUN,
            HISTORICAL_SNAPSHOT_BASELINE_LIVE,
            SNAPSHOT_INCREMENTAL_DRY_RUN,
            SNAPSHOT_INCREMENTAL_LIVE,
        }
        if mode not in quiet_modes:
            print(f"runner_arch=x64 ubuntu={release} free_disk_before_install_bytes={initial_free}")
        check_free_space(initial_free, 8 * GIB)
        password = __import__("secrets").token_urlsafe(32) + "Aa1!"
        run_sql_server_setup(password)
        after_install = free_bytes(runner_temp)
        if mode not in quiet_modes:
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
        if mode not in quiet_modes:
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
        if mode not in quiet_modes:
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
        if mode in {HISTORICAL_SNAPSHOT_BASELINE_DRY_RUN, HISTORICAL_SNAPSHOT_BASELINE_LIVE}:
            if historical_context is None:
                raise PocError("historical_manifest_context_missing")
            print("progress=historical_baseline_schema_discovery")
            restored_schema_fingerprint, provider_schemas = discover_candidate_provider_schema(
                password, entities, error_category="historical_provider_schema_mismatch"
            )
            validate_candidate_backup_identity(
                str(download["sha256"]),
                str(header_info["backup_finish_date"]),
                restored_schema_fingerprint,
                historical_context,
            )
            provider = create_candidate_sql_provider(password, entities, provider_schemas)
            print("progress=historical_baseline_start")
            if mode == HISTORICAL_SNAPSHOT_BASELINE_DRY_RUN:
                baseline_summary = run_historical_baseline_dry_run(
                    historical_context, provider, progress_callback=historical_baseline_progress_reporter()
                )
                write_historical_baseline_summary(baseline_summary)
                if historical_baseline_exit_code(baseline_summary) != 0:
                    print("restore_validation=INCOMPLETE")
                    return 2
            else:
                baseline_summary = run_historical_baseline_live(
                    historical_context,
                    provider,
                    endpoint=os.environ.get("INGEST_API_URL", ""),
                    token=os.environ.get("INGEST_API_TOKEN", ""),
                    workflow_run_id=os.environ.get("GITHUB_RUN_ID", ""),
                    progress_callback=historical_baseline_progress_reporter(),
                )
                historical_live_summary_written = True
                write_historical_baseline_live_summary(baseline_summary)
                if historical_baseline_live_exit_code(baseline_summary) != 0:
                    print("restore_validation=FAIL")
                    return 2
            print("restore_validation=PASS")
        elif mode in {SNAPSHOT_INCREMENTAL_DRY_RUN, SNAPSHOT_INCREMENTAL_LIVE}:
            if incremental_base_context is None:
                raise PocError("snapshot_incremental_base_context_missing")
            print("progress=snapshot_incremental_schema_discovery")
            restored_schema_fingerprint, _provider_schemas = discover_candidate_provider_schema(password, entities)
            if restored_schema_fingerprint != incremental_base_context["identity"]["schema_fingerprint"]:
                raise PocError("snapshot_incremental_schema_changed")
            incremental_manifest_dir = runner_temp / INCREMENTAL_MANIFEST_DIRECTORY_NAME
            current_manifest, current_manifest_info = build_current_snapshot_manifest(
                password, download, header_info, entities, runner_temp,
            )
            target_metadata = current_manifest_info["metadata"]
            validate_candidate_backup_identity(
                str(download["sha256"]),
                str(header_info["backup_finish_date"]),
                restored_schema_fingerprint,
                {"report": {"new": target_metadata, "schema_fingerprint": target_metadata["schema_fingerprint"]}},
            )
            if restored_schema_fingerprint != target_metadata["schema_fingerprint"]:
                raise PocError("snapshot_incremental_schema_changed")
            provider = create_candidate_sql_provider(password, entities, _provider_schemas)
            print("progress=snapshot_incremental_diff")
            if mode == SNAPSHOT_INCREMENTAL_DRY_RUN:
                incremental_summary = run_snapshot_incremental_dry_run(
                    incremental_base_context, current_manifest, provider, runner_temp
                )
                write_snapshot_incremental_summary(incremental_summary)
            else:
                incremental_summary = run_snapshot_incremental_live(
                    incremental_base_context, current_manifest, provider, runner_temp,
                    endpoint=os.environ.get("INGEST_API_URL", ""),
                    token=os.environ.get("INGEST_API_TOKEN", ""),
                    github_repository=os.environ.get("GITHUB_REPOSITORY", ""),
                    github_run_id=os.environ.get("GITHUB_RUN_ID", ""),
                )
                write_snapshot_incremental_live_summary(incremental_summary)
                incremental_manifest_promotable = incremental_summary.get("promote_manifest") is True
                write_incremental_promotion_output(incremental_manifest_promotable)
            if incremental_summary["status"] != "PASS":
                print("restore_validation=INCOMPLETE")
                return 2
            print("restore_validation=PASS")
        elif mode in candidate_modes:
            if candidate_context is None:
                raise PocError("candidate_manifest_context_missing")
            print("progress=candidate_schema_discovery")
            restored_schema_fingerprint, provider_schemas = discover_candidate_provider_schema(password, entities)
            validate_candidate_backup_identity(
                str(download["sha256"]),
                str(header_info["backup_finish_date"]),
                restored_schema_fingerprint,
                candidate_context,
            )
            provider = create_candidate_sql_provider(password, entities, provider_schemas)
            print("progress=candidate_extraction_start")
            candidate_result = run_candidate_extraction(
                candidate_context["old_path"],
                candidate_context["new_path"],
                provider,
                runner_temp,
                candidate_context["old_finish"],
                candidate_context["new_finish"],
                classify_candidates=mode in {CLASSIFY_SNAPSHOT_CANDIDATES, PREPARE_SNAPSHOT_INGESTION},
                prepare_ingestion=mode == PREPARE_SNAPSHOT_INGESTION,
                snapshot_ingestion_dry_run=mode == SNAPSHOT_INGESTION_DRY_RUN,
                base_snapshot=candidate_context["base_snapshot"],
                target_snapshot=candidate_context["target_snapshot"],
            )
            if mode == SNAPSHOT_INGESTION_DRY_RUN:
                write_snapshot_ingestion_client_summary(candidate_result)
                if not candidate_result["snapshot_ingestion"]["complete"]:
                    print("restore_validation=INCOMPLETE")
                    return 2
            elif mode == PREPARE_SNAPSHOT_INGESTION:
                write_snapshot_ingestion_summary(candidate_result)
                if not candidate_result["ingestion"]["complete"]:
                    print("restore_validation=INCOMPLETE")
                    return 2
            elif mode == CLASSIFY_SNAPSHOT_CANDIDATES:
                write_candidate_classification_summary(candidate_result)
                if not candidate_result["classification"]["complete"]:
                    print("restore_validation=INCOMPLETE")
                    return 2
            else:
                write_candidate_extraction_summary(candidate_result)
            print("restore_validation=PASS")
        else:
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
        if mode == HISTORICAL_SNAPSHOT_BASELINE_LIVE and not historical_live_summary_written:
            try:
                failure_summary = historical_live_failure_summary(
                    os.environ.get("GITHUB_RUN_ID", ""),
                    (historical_context or {}).get("report", {}).get("new", {}).get("backup_finish_date"),
                    exc.category,
                )
                write_historical_baseline_live_summary(failure_summary)
            except PocError:
                pass
        print(f"restore_validation=FAIL category={exc.category}", file=sys.stderr)
        return_code = 2
    except Exception:
        if mode == HISTORICAL_SNAPSHOT_BASELINE_LIVE and not historical_live_summary_written:
            try:
                failure_summary = historical_live_failure_summary(
                    os.environ.get("GITHUB_RUN_ID", ""),
                    (historical_context or {}).get("report", {}).get("new", {}).get("backup_finish_date"),
                    "unexpected_internal_error",
                )
                write_historical_baseline_live_summary(failure_summary)
            except PocError:
                pass
        print("restore_validation=FAIL category=unexpected_internal_error", file=sys.stderr)
        return_code = 2
    else:
        return_code = 0
    finally:
        os.environ.pop("ODA_BACKUP_USERNAME", None)
        os.environ.pop("ODA_BACKUP_PASSWORD", None)
        os.environ.pop("INGEST_API_TOKEN", None)
        try:
            if (incremental_manifest_dir is not None and runner_temp is not None
                    and not incremental_manifest_promotable):
                cleanup_incremental_manifest(incremental_manifest_dir, runner_temp)
        except PocError as exc:
            print(f"cleanup=FAIL category={exc.category}", file=sys.stderr)
            return_code = 2
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
