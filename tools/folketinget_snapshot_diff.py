#!/usr/bin/env python3
"""Read-only aggregate diff for validated Folketinget SQLite manifests."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

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
        if tables != set(EXPECTED_TABLE_COLUMNS):
            raise DiffError("manifest_structure_invalid")
        if any(_table_columns(connection, table) != expected for table, expected in EXPECTED_TABLE_COLUMNS.items()):
            raise DiffError("manifest_structure_invalid")

        # Confirm the v1 primary-key and WITHOUT ROWID layout, not just column names.
        try:
            rows_info = connection.execute('PRAGMA table_info("rows")').fetchall()
            counts_info = connection.execute('PRAGMA table_info("entity_counts")').fetchall()
            metadata_info = connection.execute('PRAGMA table_info("snapshot_metadata")').fetchall()
        except sqlite3.Error:
            raise DiffError("manifest_structure_invalid") from None
        if [(row[1], row[5]) for row in rows_info] != [("entity", 1), ("canonical_key", 2), ("full_hash", 0), ("semantic_hash", 0)]:
            raise DiffError("manifest_structure_invalid")
        if [(row[1], row[5]) for row in counts_info] != [("entity", 1), ("row_count", 0)]:
            raise DiffError("manifest_structure_invalid")
        if [(row[1], row[5]) for row in metadata_info] != [("singleton", 1), ("backup_sha256", 0), ("backup_finish_date", 0), ("source_sql_major", 0), ("schema_fingerprint", 0), ("generated_at", 0), ("total_rows", 0)]:
            raise DiffError("manifest_structure_invalid")
        if any(tuple(row[2].upper() for row in info) != EXPECTED_COLUMN_TYPES[table]
               for table, info in (("snapshot_metadata", metadata_info), ("entity_counts", counts_info), ("rows", rows_info))):
            raise DiffError("manifest_structure_invalid")
        try:
            ddl = {name: sql.upper() for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN ('entity_counts','rows')"
            )}
        except sqlite3.Error:
            raise DiffError("manifest_structure_invalid") from None
        if any("WITHOUT ROWID" not in ddl.get(table, "") for table in ("entity_counts", "rows")):
            raise DiffError("manifest_structure_invalid")

        try:
            metadata_rows = connection.execute(
                "SELECT singleton, backup_sha256, backup_finish_date, source_sql_major, schema_fingerprint, generated_at, total_rows FROM snapshot_metadata"
            ).fetchall()
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
        singleton, backup_sha, finish_date, sql_major, fingerprint, generated_at, declared_total = metadata_rows[0]
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
        return {
            "backup_sha256": backup_sha,
            "backup_finish_date": finish_date,
            "schema_fingerprint": fingerprint,
            "total_rows": total,
        }
    finally:
        connection.close()


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
