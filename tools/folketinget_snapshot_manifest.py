#!/usr/bin/env python3
"""Build a compact, row-content-free manifest from restored ODA tables."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import sqlite3
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable


REQUIRED_ENTITIES = ("Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus")
DEFAULT_BATCH_SIZE = 5000
MAX_BATCH_JSON_BYTES = 64 * 1024 * 1024
SUPPORTED_SQL_TYPES = frozenset(
    {
        "bigint", "int", "smallint", "tinyint", "bit", "decimal", "numeric", "money", "smallmoney",
        "float", "real", "char", "varchar", "nchar", "nvarchar", "text", "ntext", "uniqueidentifier",
        "date", "datetime", "datetime2", "smalldatetime", "time", "datetimeoffset", "binary", "varbinary",
        "image", "timestamp", "rowversion",
    }
)
SEMANTIC_EXCLUDED_COLUMNS = frozenset({"opdateringsdato"})


class ManifestError(Exception):
    """Safe manifest failure category; never include row values in messages."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ManifestError("unsupported_value_type")
    if value.is_zero():
        return "0"
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return {"$decimal": _decimal_text(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ManifestError("unsupported_value_type")
        return {"$decimal": _decimal_text(Decimal(str(value)))}
    if isinstance(value, bytes):
        return {"$binary": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat(timespec="microseconds")}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, time):
        return {"$time": value.isoformat(timespec="microseconds")}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ManifestError("unsupported_value_type")
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    raise ManifestError("unsupported_value_type")


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            _canonical_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ManifestError("unsupported_value_type") from None


def _sql_value(value: Any, type_name: str) -> Any:
    sql_type = type_name.casefold()
    if sql_type not in SUPPORTED_SQL_TYPES:
        raise ManifestError("unsupported_sql_type")
    if value is None:
        return None
    if sql_type in {"binary", "varbinary", "image", "timestamp", "rowversion"}:
        if not isinstance(value, str):
            raise ManifestError("manifest_row_invalid")
        try:
            return base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            raise ManifestError("manifest_row_invalid") from None
    if sql_type == "bit":
        if not isinstance(value, bool):
            raise ManifestError("manifest_row_invalid")
        return value
    if sql_type in {"tinyint", "smallint", "int", "bigint"}:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ManifestError("manifest_row_invalid")
        return value
    if sql_type in {"decimal", "numeric", "money", "smallmoney", "float", "real"}:
        if isinstance(value, bool) or not isinstance(value, (int, Decimal, str)):
            raise ManifestError("manifest_row_invalid")
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise ManifestError("manifest_row_invalid") from None
        if not number.is_finite():
            raise ManifestError("manifest_row_invalid")
        return number
    if sql_type == "uniqueidentifier":
        if not isinstance(value, str):
            raise ManifestError("manifest_row_invalid")
        return value.casefold()
    if sql_type in {"date", "datetime", "datetime2", "smalldatetime", "time", "datetimeoffset"}:
        if not isinstance(value, str) or not value:
            raise ManifestError("manifest_row_invalid")
        # Preserve SQL's ISO text exactly, including datetime2's seventh digit.
        return value
    if not isinstance(value, str):
        raise ManifestError("manifest_row_invalid")
    return value


def normalize_schema(entity: str, schema: dict[str, Any]) -> dict[str, Any]:
    columns = schema.get("columns")
    if not isinstance(columns, list) or not columns:
        raise ManifestError("manifest_schema_invalid")
    normalized_columns: list[dict[str, Any]] = []
    names: set[str] = set()
    if any(not isinstance(column, dict) for column in columns):
        raise ManifestError("manifest_schema_invalid")
    try:
        ordered_input = sorted(columns, key=lambda item: int(item.get("ordinal", 0)))
    except (TypeError, ValueError, OverflowError):
        raise ManifestError("manifest_schema_invalid") from None
    ordinals: set[int] = set()
    for column in ordered_input:
        try:
            name = column["name"]
            type_name = str(column["type_name"]).casefold()
            ordinal = int(column["ordinal"])
            max_length = int(column["max_length"])
            precision = int(column["precision"])
            scale = int(column["scale"])
            nullable = int(column["nullable"])
            key_ordinal = int(column.get("key_ordinal") or 0)
            collation = column.get("collation") or None
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ManifestError("manifest_schema_invalid") from None
        if not isinstance(name, str) or not name or ordinal <= 0 or ordinal in ordinals or nullable not in {0, 1} or key_ordinal < 0:
            raise ManifestError("manifest_schema_invalid")
        ordinals.add(ordinal)
        if name.casefold() in names:
            raise ManifestError("manifest_schema_invalid")
        names.add(name.casefold())
        if type_name not in SUPPORTED_SQL_TYPES:
            raise ManifestError("unsupported_sql_type")
        normalized_columns.append(
            {
                "ordinal": ordinal,
                "name": name,
                "type_name": type_name,
                "max_length": max_length,
                "precision": precision,
                "scale": scale,
                "nullable": nullable,
                "key_ordinal": key_ordinal,
                "collation": collation,
            }
        )
    if ordinals != set(range(1, len(normalized_columns) + 1)):
        raise ManifestError("manifest_schema_invalid")
    primary_key = [column["name"] for column in sorted(normalized_columns, key=lambda item: item["key_ordinal"]) if column["key_ordinal"]]
    if not primary_key or sorted(column["key_ordinal"] for column in normalized_columns if column["key_ordinal"]) != list(range(1, len(primary_key) + 1)):
        raise ManifestError("manifest_primary_key_missing_or_invalid")
    for key_name in primary_key:
        key_column = next(column for column in normalized_columns if column["name"] == key_name)
        if key_column["nullable"]:
            raise ManifestError("manifest_primary_key_missing_or_invalid")
    schema_name = schema.get("schema_name")
    if not isinstance(schema_name, str) or not schema_name:
        raise ManifestError("manifest_schema_invalid")
    if schema.get("object_type") != "USER_TABLE":
        raise ManifestError("manifest_primary_key_missing_or_invalid")
    return {"entity": entity, "schema_name": schema_name, "primary_key": primary_key, "columns": normalized_columns}


def schema_fingerprint(schemas: dict[str, dict[str, Any]]) -> str:
    if tuple(schemas) != REQUIRED_ENTITIES:
        raise ManifestError("manifest_required_entity_missing")
    normalized = [normalize_schema(entity, schemas[entity]) for entity in REQUIRED_ENTITIES]
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


def _row_hash_inputs(entity: str, schema: dict[str, Any], row: dict[str, Any]) -> tuple[str, bytes, bytes]:
    columns = schema["columns"]
    aliases = [f"c{index:04d}" for index in range(len(columns))]
    if set(row) != set(aliases):
        raise ManifestError("manifest_row_invalid")
    values: dict[str, Any] = {}
    for alias, column in zip(aliases, columns):
        values[column["name"]] = _sql_value(row[alias], column["type_name"])
    by_name = {column["name"]: column for column in columns}
    key_values = [
        [name, by_name[name]["type_name"], values[name]]
        for name in schema["primary_key"]
    ]
    if any(value[2] is None for value in key_values):
        raise ManifestError("manifest_row_invalid")
    canonical_key = canonical_json_bytes({"entity": entity, "key": key_values}).decode("utf-8")
    full_fields = [[column["name"], column["type_name"], values[column["name"]]] for column in columns]
    semantic_fields = [
        field for field in full_fields if field[0].casefold() not in SEMANTIC_EXCLUDED_COLUMNS
    ]
    full_hash = hashlib.sha256(canonical_json_bytes({"entity": entity, "row": full_fields})).digest()
    semantic_hash = hashlib.sha256(canonical_json_bytes({"entity": entity, "row": semantic_fields})).digest()
    return canonical_key, full_hash, semantic_hash


def _decode_json_page(payload: str | bytes) -> list[dict[str, Any]]:
    if isinstance(payload, bytes):
        if len(payload) > MAX_BATCH_JSON_BYTES:
            raise ManifestError("manifest_page_too_large")
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise ManifestError("manifest_page_invalid") from None
    try:
        if len(payload.encode("utf-8")) > MAX_BATCH_JSON_BYTES:
            raise ManifestError("manifest_page_too_large")
    except UnicodeEncodeError:
        raise ManifestError("manifest_page_invalid") from None
    try:
        value = json.loads(payload, parse_float=Decimal, parse_int=int, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (json.JSONDecodeError, ValueError, TypeError):
        raise ManifestError("manifest_page_invalid") from None
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ManifestError("manifest_page_invalid")
    return value


def build_manifest(
    manifest_path: Path,
    *,
    backup_sha256: str,
    backup_finish_date: str,
    source_sql_major: int,
    schemas: dict[str, dict[str, Any]],
    expected_counts: dict[str, int],
    fetch_page: Callable[[str, str, dict[str, Any], int, int], str | bytes],
    batch_size: int = DEFAULT_BATCH_SIZE,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if tuple(schemas) != REQUIRED_ENTITIES or tuple(expected_counts) != REQUIRED_ENTITIES:
        raise ManifestError("manifest_required_entity_missing")
    if not isinstance(backup_sha256, str) or len(backup_sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in backup_sha256):
        raise ManifestError("manifest_metadata_invalid")
    if not isinstance(source_sql_major, int) or source_sql_major <= 0 or not isinstance(backup_finish_date, str):
        raise ManifestError("manifest_metadata_invalid")
    if not isinstance(batch_size, int) or batch_size <= 0 or batch_size > DEFAULT_BATCH_SIZE:
        raise ManifestError("manifest_batch_size_invalid")
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    normalized_schemas = {entity: normalize_schema(entity, schemas[entity]) for entity in REQUIRED_ENTITIES}
    fingerprint = hashlib.sha256(canonical_json_bytes([normalized_schemas[entity] for entity in REQUIRED_ENTITIES])).hexdigest()
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if manifest_path.exists():
        raise ManifestError("manifest_path_exists")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(manifest_path)
        os.chmod(manifest_path, 0o600)
        connection.execute("PRAGMA page_size=4096")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(
            "CREATE TABLE snapshot_metadata ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1), backup_sha256 TEXT NOT NULL, "
            "backup_finish_date TEXT NOT NULL, source_sql_major INTEGER NOT NULL, "
            "schema_fingerprint TEXT NOT NULL, generated_at TEXT NOT NULL, total_rows INTEGER NOT NULL);"
            "CREATE TABLE entity_counts (entity TEXT PRIMARY KEY, row_count INTEGER NOT NULL) WITHOUT ROWID;"
            'CREATE TABLE "rows" (entity TEXT NOT NULL, canonical_key TEXT NOT NULL, '
            "full_hash BLOB NOT NULL CHECK(length(full_hash)=32), "
            "semantic_hash BLOB NOT NULL CHECK(length(semantic_hash)=32), "
            "PRIMARY KEY(entity, canonical_key)) WITHOUT ROWID;"
        )
        total_rows = 0
        per_entity: dict[str, int] = {}
        for entity in REQUIRED_ENTITIES:
            schema = normalized_schemas[entity]
            expected = expected_counts[entity]
            if not isinstance(expected, int) or expected < 0:
                raise ManifestError("manifest_expected_count_invalid")
            offset = 0
            count = 0
            while True:
                payload = fetch_page(entity, schema["schema_name"], schema, offset, batch_size)
                page = _decode_json_page(payload)
                if len(page) > batch_size:
                    raise ManifestError("manifest_page_invalid")
                if not page:
                    break
                try:
                    with connection:
                        for row in page:
                            canonical_key, full_hash, semantic_hash = _row_hash_inputs(entity, schema, row)
                            connection.execute(
                                'INSERT INTO "rows" (entity, canonical_key, full_hash, semantic_hash) VALUES (?, ?, ?, ?)',
                                (entity, canonical_key, full_hash, semantic_hash),
                            )
                except sqlite3.IntegrityError:
                    raise ManifestError("duplicate_canonical_key") from None
                except ManifestError:
                    raise
                except (sqlite3.Error, OverflowError):
                    raise ManifestError("manifest_write_failed") from None
                count += len(page)
                offset += len(page)
                if count > expected:
                    raise ManifestError("manifest_row_count_mismatch")
                if len(page) < batch_size:
                    break
            if count != expected:
                raise ManifestError("manifest_row_count_mismatch")
            connection.execute("INSERT INTO entity_counts(entity,row_count) VALUES (?,?)", (entity, count))
            total_rows += count
            per_entity[entity] = count
        connection.execute(
            "INSERT INTO snapshot_metadata VALUES (1,?,?,?,?,?,?)",
            (backup_sha256.lower(), backup_finish_date, source_sql_major, fingerprint, generated_at, total_rows),
        )
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise ManifestError("manifest_integrity_check_failed")
        stored_total = connection.execute('SELECT COUNT(*) FROM "rows"').fetchone()[0]
        if stored_total != total_rows:
            raise ManifestError("manifest_row_count_mismatch")
        connection.execute("VACUUM")
        connection.close()
        connection = None
        os.chmod(manifest_path, 0o600)
        return {
            "schema_fingerprint": fingerprint,
            "total_rows": total_rows,
            "entity_counts": per_entity,
            "manifest_bytes": manifest_path.stat().st_size,
            "validation": "PASS",
        }
    except ManifestError:
        if connection is not None:
            connection.close()
        manifest_path.unlink(missing_ok=True)
        raise
    except (sqlite3.Error, OSError, OverflowError):
        if connection is not None:
            connection.close()
        manifest_path.unlink(missing_ok=True)
        raise ManifestError("manifest_write_failed") from None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise ManifestError("manifest_read_failed") from None
    return digest.hexdigest()
