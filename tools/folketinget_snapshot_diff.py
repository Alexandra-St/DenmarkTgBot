#!/usr/bin/env python3
"""Read-only aggregate diff for validated Folketinget SQLite manifests."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterator

TOOLS_DIRECTORY = str(Path(__file__).resolve().parent)
if TOOLS_DIRECTORY not in sys.path:
    sys.path.insert(0, TOOLS_DIRECTORY)
import folketinget_snapshot_manifest as manifest


FORMAT_VERSION = 1  # Defined by the exact v1 table/column layout below.
CATEGORIES = ("added", "removed", "changed", "technical_only", "unchanged")
HEX_256 = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_TABLE_COLUMNS = {
    "snapshot_metadata": ("singleton", "backup_sha256", "backup_finish_date", "source_sql_major", "schema_fingerprint", "generated_at", "total_rows"),
    "entity_counts": ("entity", "row_count"),
    "rows": ("entity", "canonical_key", "full_hash", "semantic_hash"),
}
EXPECTED_COLUMN_TYPES = {
    "snapshot_metadata": ("INTEGER", "TEXT", "TEXT", "INTEGER", "TEXT", "TEXT", "INTEGER"),
    "entity_counts": ("TEXT", "INTEGER"),
    "rows": ("TEXT", "TEXT", "BLOB", "BLOB"),
}
V2_METADATA_COLUMNS = EXPECTED_TABLE_COLUMNS["snapshot_metadata"] + ("manifest_format_version", "routing_row_count")
ROUTING_COLUMNS = ("entity", "row_id", "sagid", "dokumentid", "sagstrinid", "statusid")
ROUTING_INDEXES = {"routing_sagid_idx", "routing_dokumentid_idx", "routing_sagstrinid_idx", "routing_statusid_idx"}
ROUTING_INDEX_COLUMNS = {
    "routing_sagid_idx": ("sagid",),
    "routing_dokumentid_idx": ("dokumentid",),
    "routing_sagstrinid_idx": ("sagstrinid",),
    "routing_statusid_idx": ("statusid",),
}


class DiffError(Exception):
    """Safe diff failure category."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    try:
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        return connection
    except (OSError, sqlite3.Error, ValueError):
        raise DiffError("manifest_open_failed") from None


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    try:
        return tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))
    except sqlite3.Error:
        raise DiffError("manifest_structure_invalid") from None


def validate_manifest(path: Path) -> dict[str, Any]:
    connection = _readonly_connection(path)
    try:
        try:
            integrity_cursor = connection.execute("PRAGMA integrity_check")
            integrity = integrity_cursor.fetchone()
            has_additional_errors = integrity_cursor.fetchone() is not None
        except sqlite3.Error:
            raise DiffError("manifest_integrity_check_failed") from None
        if integrity != ("ok",) or has_additional_errors:
            raise DiffError("manifest_integrity_check_failed")

        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
        except sqlite3.Error:
            raise DiffError("manifest_structure_invalid") from None
        metadata_columns = _table_columns(connection, "snapshot_metadata")
        if metadata_columns == EXPECTED_TABLE_COLUMNS["snapshot_metadata"]:
            format_version = 1
            expected_tables = set(EXPECTED_TABLE_COLUMNS)
        elif metadata_columns == V2_METADATA_COLUMNS:
            format_version = 2
            expected_tables = set(EXPECTED_TABLE_COLUMNS) | {"routing"}
        else:
            raise DiffError("manifest_structure_invalid")
        if tables != expected_tables:
            raise DiffError("manifest_structure_invalid")
        if any(_table_columns(connection, table) != expected for table, expected in EXPECTED_TABLE_COLUMNS.items() if table != "snapshot_metadata"):
            raise DiffError("manifest_structure_invalid")
        if format_version == 2 and _table_columns(connection, "routing") != ROUTING_COLUMNS:
            raise DiffError("manifest_structure_invalid")

        # Confirm the v1 primary-key and WITHOUT ROWID layout, not just column names.
        try:
            rows_info = connection.execute('PRAGMA table_info("rows")').fetchall()
            counts_info = connection.execute('PRAGMA table_info("entity_counts")').fetchall()
            metadata_info = connection.execute('PRAGMA table_info("snapshot_metadata")').fetchall()
            routing_info = connection.execute('PRAGMA table_info("routing")').fetchall() if format_version == 2 else []
        except sqlite3.Error:
            raise DiffError("manifest_structure_invalid") from None
        if [(row[1], row[5]) for row in rows_info] != [("entity", 1), ("canonical_key", 2), ("full_hash", 0), ("semantic_hash", 0)]:
            raise DiffError("manifest_structure_invalid")
        if [(row[1], row[5]) for row in counts_info] != [("entity", 1), ("row_count", 0)]:
            raise DiffError("manifest_structure_invalid")
        expected_metadata_pk = [("singleton", 1)] + [(name, 0) for name in metadata_columns[1:]]
        if [(row[1], row[5]) for row in metadata_info] != expected_metadata_pk:
            raise DiffError("manifest_structure_invalid")
        expected_metadata_types = EXPECTED_COLUMN_TYPES["snapshot_metadata"] + (("INTEGER", "INTEGER") if format_version == 2 else ())
        if (tuple(row[2].upper() for row in metadata_info) != expected_metadata_types
                or any(tuple(row[2].upper() for row in info) != EXPECTED_COLUMN_TYPES[table]
                       for table, info in (("entity_counts", counts_info), ("rows", rows_info)))):
            raise DiffError("manifest_structure_invalid")
        if format_version == 2:
            if ([(row[1], row[5]) for row in routing_info] != [("entity", 1), ("row_id", 2)] + [(name, 0) for name in ROUTING_COLUMNS[2:]]
                    or tuple(row[2].upper() for row in routing_info) != ("TEXT", "INTEGER", "INTEGER", "INTEGER", "INTEGER", "INTEGER")):
                raise DiffError("manifest_structure_invalid")
        try:
            ddl = {name: sql.upper() for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN ('entity_counts','rows','routing')"
            )}
        except sqlite3.Error:
            raise DiffError("manifest_structure_invalid") from None
        expected_without_rowid = ("entity_counts", "rows", "routing") if format_version == 2 else ("entity_counts", "rows")
        if any("WITHOUT ROWID" not in ddl.get(table, "") for table in expected_without_rowid):
            raise DiffError("manifest_structure_invalid")

        try:
            metadata_rows = connection.execute("SELECT * FROM snapshot_metadata").fetchall()
            entity_counts = connection.execute("SELECT entity, row_count FROM entity_counts ORDER BY entity").fetchall()
            actual_counts = connection.execute('SELECT entity, COUNT(*) FROM "rows" GROUP BY entity ORDER BY entity').fetchall()
            total = connection.execute('SELECT COUNT(*) FROM "rows"').fetchone()[0]
            malformed = connection.execute(
                'SELECT 1 FROM "rows" WHERE typeof(entity)!="text" OR typeof(canonical_key)!="text" '
                'OR typeof(full_hash)!="blob" OR length(full_hash)!=32 '
                'OR typeof(semantic_hash)!="blob" OR length(semantic_hash)!=32 LIMIT 1'
            ).fetchone()
        except sqlite3.Error:
            raise DiffError("manifest_structure_invalid") from None
        if len(metadata_rows) != 1:
            raise DiffError("manifest_structure_invalid")
        singleton, backup_sha, finish_date, sql_major, fingerprint, generated_at, declared_total = metadata_rows[0][:7]
        if {entity for entity, _ in entity_counts} != set(manifest.REQUIRED_ENTITIES):
            raise DiffError("manifest_required_entities_invalid")
        if not {entity for entity, _ in actual_counts}.issubset(set(manifest.REQUIRED_ENTITIES)):
            raise DiffError("manifest_required_entities_invalid")
        actual_count_map = {entity: count for entity, count in actual_counts}
        if (any(not isinstance(count, int) or count < 0 for _, count in entity_counts)
                or dict(entity_counts) != {entity: actual_count_map.get(entity, 0) for entity in manifest.REQUIRED_ENTITIES}):
            raise DiffError("manifest_row_count_mismatch")
        if malformed or declared_total != total or sum(count for _, count in entity_counts) != total:
            raise DiffError("manifest_row_count_mismatch")
        if (singleton != 1 or not isinstance(backup_sha, str) or not HEX_256.fullmatch(backup_sha)
                or not isinstance(finish_date, str) or not finish_date
                or not isinstance(sql_major, int) or sql_major <= 0
                or not isinstance(fingerprint, str) or not HEX_256.fullmatch(fingerprint)
                or not isinstance(generated_at, str) or not generated_at
                or not isinstance(declared_total, int) or declared_total < 0):
            raise DiffError("manifest_metadata_invalid")
        routing_available = format_version == 2
        if routing_available:
            manifest_version, declared_routing = metadata_rows[0][-2:]
            actual_routing = connection.execute("SELECT COUNT(*) FROM routing").fetchone()[0]
            route_counts = dict(connection.execute("SELECT entity,COUNT(*) FROM routing GROUP BY entity"))
            if (manifest_version != 2 or declared_routing != total or actual_routing != total
                    or {entity: route_counts.get(entity, 0) for entity in manifest.REQUIRED_ENTITIES}
                    != {entity: actual_count_map.get(entity, 0) for entity in manifest.REQUIRED_ENTITIES}):
                raise DiffError("manifest_routing_invalid")
            indexes = {row[1] for row in connection.execute('PRAGMA index_list("routing")')}
            if not ROUTING_INDEXES.issubset(indexes):
                raise DiffError("manifest_routing_invalid")
            for index_name, expected_columns in ROUTING_INDEX_COLUMNS.items():
                actual_columns = tuple(row[2] for row in connection.execute(f'PRAGMA index_info("{index_name}")'))
                if actual_columns != expected_columns:
                    raise DiffError("manifest_routing_invalid")
            allowed = {entity: {"Sag": ("statusid",), "Sagstrin": ("sagid",), "SagDokument": ("sagid", "dokumentid"),
                                "Dokument": (), "Afstemning": ("sagstrinid",), "Sagsstatus": ()}[entity]
                       for entity in manifest.REQUIRED_ENTITIES}
            try:
                cursor = connection.execute('SELECT r.entity,r.canonical_key,t.entity,t.row_id,t.sagid,t.dokumentid,t.sagstrinid,t.statusid '
                                            'FROM "rows" r LEFT JOIN routing t ON t.entity=r.entity '
                                            "AND t.row_id=json_extract(r.canonical_key,'$.key[0][2]') ORDER BY r.entity,r.canonical_key")
                batches = iter(lambda: cursor.fetchmany(5000), [])
                for batch in batches:
                    for entity, key, route_entity, row_id, sagid, dokumentid, sagstrinid, statusid in batch:
                        try:
                            parsed = json.loads(key)
                            key_fields = parsed["key"]
                            if (not isinstance(parsed, dict) or set(parsed) != {"entity", "key"} or parsed.get("entity") != entity
                                    or not isinstance(key_fields, list) or len(key_fields) != 1
                                    or not isinstance(key_fields[0], list) or len(key_fields[0]) != 3 or key_fields[0][0] != "id"
                                    or key_fields[0][1] not in {"tinyint", "smallint", "int", "bigint"}
                                    or isinstance(key_fields[0][2], bool) or not isinstance(key_fields[0][2], int)
                                    or json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != key):
                                raise ValueError
                            decoded = key_fields[0][2]
                        except (TypeError, ValueError, KeyError, IndexError):
                            raise DiffError("manifest_routing_invalid") from None
                        if (route_entity != entity or isinstance(row_id, bool) or not isinstance(row_id, int) or row_id != decoded
                                or isinstance(decoded, bool) or not isinstance(decoded, int)):
                            raise DiffError("manifest_routing_invalid")
                        values = {"sagid": sagid, "dokumentid": dokumentid, "sagstrinid": sagstrinid, "statusid": statusid}
                        if any(value is not None and (isinstance(value, bool) or not isinstance(value, int)) for value in values.values()):
                            raise DiffError("manifest_routing_invalid")
                        if any(values[name] is not None for name in values if name not in allowed[entity]):
                            raise DiffError("manifest_routing_invalid")
            except sqlite3.Error:
                raise DiffError("manifest_routing_invalid") from None
        return {
            "backup_sha256": backup_sha,
            "backup_finish_date": finish_date,
            "schema_fingerprint": fingerprint,
            "total_rows": total,
            "manifest_format_version": format_version,
            "routing_available": routing_available,
        }
    finally:
        connection.close()


class ManifestRoutingReader:
    """Read validated old-side routing integers without exposing source rows."""

    _reverse_fields = {
        "SagDokument": frozenset({"dokumentid"}),
        "Afstemning": frozenset({"sagstrinid"}),
        "Sag": frozenset({"statusid"}),
    }

    def __init__(self, path: Path) -> None:
        self._path = _manifest_file(path)
        metadata = validate_manifest(self._path)
        if not metadata["routing_available"]:
            raise DiffError("manifest_routing_unavailable")
        self._connection = _readonly_connection(self._path)

    def close(self) -> None:
        self._connection.close()

    def get_row(self, entity: str, row_id: int) -> dict[str, int | None] | None:
        if entity not in manifest.REQUIRED_ENTITIES or isinstance(row_id, bool) or not isinstance(row_id, int):
            raise DiffError("manifest_routing_lookup_invalid")
        try:
            row = self._connection.execute(
                "SELECT sagid,dokumentid,sagstrinid,statusid FROM routing WHERE entity=? AND row_id=?",
                (entity, row_id),
            ).fetchone()
        except sqlite3.Error:
            raise DiffError("manifest_routing_invalid") from None
        if row is None:
            return None
        return dict(zip(("sagid", "dokumentid", "sagstrinid", "statusid"), row))

    def iter_by_field(self, entity: str, field: str, value: int) -> Iterator[dict[str, int | None]]:
        if (entity not in self._reverse_fields or field not in self._reverse_fields[entity]
                or isinstance(value, bool) or not isinstance(value, int)):
            raise DiffError("manifest_routing_lookup_invalid")
        try:
            cursor = self._connection.execute(
                f"SELECT row_id,sagid,dokumentid,sagstrinid,statusid FROM routing WHERE entity=? AND {field}=? ORDER BY row_id",
                (entity, value),
            )
            while batch := cursor.fetchmany(1000):
                for row_id, sagid, dokumentid, sagstrinid, statusid in batch:
                    yield dict(zip(("id", "sagid", "dokumentid", "sagstrinid", "statusid"),
                                   (row_id, sagid, dokumentid, sagstrinid, statusid)))
        except sqlite3.Error:
            raise DiffError("manifest_routing_invalid") from None


def _manifest_file(value: Path) -> Path:
    if value.is_file() and not value.is_symlink():
        return value
    if value.is_dir() and not value.is_symlink():
        try:
            children = list(value.iterdir())
        except OSError:
            raise DiffError("manifest_artifact_invalid") from None
        if len(children) == 1 and children[0].name == "manifest.sqlite" and children[0].is_file() and not children[0].is_symlink():
            return children[0]
    raise DiffError("manifest_artifact_invalid")


def _counts_for_pair(old_path: Path, new_path: Path) -> dict[str, dict[str, int]]:
    connection = _readonly_connection(old_path)
    try:
        try:
            new_uri = new_path.resolve(strict=True).as_uri() + "?mode=ro"
            connection.execute("ATTACH DATABASE ? AS newer", (new_uri,))
            query = '''
                SELECT entity, category, COUNT(*) FROM (
                  SELECT o.entity AS entity,
                    CASE WHEN o.semantic_hash != n.semantic_hash THEN 'changed'
                         WHEN o.full_hash != n.full_hash THEN 'technical_only'
                         ELSE 'unchanged' END AS category
                  FROM main."rows" o JOIN newer."rows" n
                    ON n.entity=o.entity AND n.canonical_key=o.canonical_key
                  UNION ALL
                  SELECT o.entity, 'removed' FROM main."rows" o
                    LEFT JOIN newer."rows" n ON n.entity=o.entity AND n.canonical_key=o.canonical_key
                    WHERE n.canonical_key IS NULL
                  UNION ALL
                  SELECT n.entity, 'added' FROM newer."rows" n
                    LEFT JOIN main."rows" o ON o.entity=n.entity AND o.canonical_key=n.canonical_key
                    WHERE o.canonical_key IS NULL
                ) GROUP BY entity, category
            '''
            result = connection.execute(query).fetchall()
        except (OSError, sqlite3.Error, ValueError):
            raise DiffError("manifest_diff_query_failed") from None
        counts = {entity: {category: 0 for category in CATEGORIES} for entity in manifest.REQUIRED_ENTITIES}
        for entity, category, count in result:
            if entity not in counts or category not in CATEGORIES or not isinstance(count, int) or count < 0:
                raise DiffError("manifest_diff_result_invalid")
            counts[entity][category] = count
        return counts
    finally:
        connection.close()


def _iter_manifest_changes_validated(old_path: Path, new_path: Path) -> Iterator[tuple[str, str, str]]:
    """Yield (entity, category, canonical_key) for non-unchanged rows only.

    Caller must first validate both manifests and their matching schema fingerprints.
    The cursor is consumed in bounded batches; canonical keys never enter public output.
    """
    connection = _readonly_connection(old_path)
    try:
        try:
            new_uri = new_path.resolve(strict=True).as_uri() + "?mode=ro"
            connection.execute("ATTACH DATABASE ? AS newer", (new_uri,))
            cursor = connection.execute('''
                SELECT entity, category, canonical_key FROM (
                  SELECT o.entity AS entity,
                    CASE WHEN o.semantic_hash != n.semantic_hash THEN 'changed'
                         WHEN o.full_hash != n.full_hash THEN 'technical_only' END AS category,
                    o.canonical_key AS canonical_key
                  FROM main."rows" o JOIN newer."rows" n
                    ON n.entity=o.entity AND n.canonical_key=o.canonical_key
                  WHERE o.semantic_hash != n.semantic_hash OR o.full_hash != n.full_hash
                  UNION ALL
                  SELECT o.entity, 'removed', o.canonical_key FROM main."rows" o
                    LEFT JOIN newer."rows" n ON n.entity=o.entity AND n.canonical_key=o.canonical_key
                    WHERE n.canonical_key IS NULL
                  UNION ALL
                  SELECT n.entity, 'added', n.canonical_key FROM newer."rows" n
                    LEFT JOIN main."rows" o ON o.entity=n.entity AND o.canonical_key=n.canonical_key
                    WHERE o.canonical_key IS NULL
                ) ORDER BY entity, category, canonical_key
            ''')
            while batch := cursor.fetchmany(1000):
                for entity, category, canonical_key in batch:
                    if entity not in manifest.REQUIRED_ENTITIES or category not in {"added", "changed", "technical_only", "removed"} or not isinstance(canonical_key, str):
                        raise DiffError("manifest_diff_result_invalid")
                    yield entity, category, canonical_key
        except DiffError:
            raise
        except (OSError, sqlite3.Error, ValueError):
            raise DiffError("manifest_diff_query_failed") from None
    finally:
        connection.close()


def _iter_manifest_changes(old_path: Path, new_path: Path) -> Iterator[tuple[str, str, str]]:
    """Validated private key-level iterator; never use its keys in logs or reports."""
    old = validate_manifest(old_path)
    new = validate_manifest(new_path)
    if old["schema_fingerprint"] != new["schema_fingerprint"]:
        raise DiffError("manifest_schema_mismatch")
    yield from _iter_manifest_changes_validated(old_path, new_path)


def compare_manifests(old_value: Path, new_value: Path) -> dict[str, Any]:
    old_path = _manifest_file(old_value)
    new_path = _manifest_file(new_value)
    old = validate_manifest(old_path)
    new = validate_manifest(new_path)
    if old["schema_fingerprint"] != new["schema_fingerprint"]:
        raise DiffError("manifest_schema_mismatch")
    by_entity = _counts_for_pair(old_path, new_path)
    totals = {category: sum(by_entity[entity][category] for entity in manifest.REQUIRED_ENTITIES) for category in CATEGORIES}
    return {
        "manifest_format_version": FORMAT_VERSION,
        "old": {"backup_sha256": old["backup_sha256"], "backup_finish_date": old["backup_finish_date"]},
        "new": {"backup_sha256": new["backup_sha256"], "backup_finish_date": new["backup_finish_date"]},
        "schema_fingerprint": old["schema_fingerprint"],
        "entities": by_entity,
        "totals": totals,
        "validation": "PASS",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", required=True, type=Path, help="Old manifest.sqlite or its extracted artifact directory")
    parser.add_argument("--new", required=True, type=Path, help="New manifest.sqlite or its extracted artifact directory")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(compare_manifests(args.old, args.new), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except DiffError as error:
        print(f"snapshot_diff=FAIL category={error.category}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
