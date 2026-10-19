"""Shared, pure SQL Server query helpers for the Folketinget PoCs.

This module intentionally imports neither the backup validator nor the SQL
provider, so both can use the same bounded transport without an import cycle.
"""

from __future__ import annotations

MANIFEST_BASE64_CHUNK_SIZE = 6000
# 64 MiB UTF-8 JSON needs at most 128 MiB UTF-16LE, or 29,827 chunks.
MANIFEST_MAX_BASE64_CHUNKS = 30000
# Five sequence digits + separator + 6000 Base64 chars = at most 6006 chars.
MANIFEST_SQL_ROW_WIDTH = 6100


def quote_identifier(value: str) -> str:
    """Quote a SQL identifier; callers must map ValueError to a safe error."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid SQL identifier")
    escaped = value.replace("]", "]]" )
    return "[" + escaped + "]"


def build_chunked_json_query(select_query: str, *, variable: str) -> str:
    """Wrap a trusted JSON SELECT in the fixed-width Base64 chunk protocol."""
    if variable not in {"manifest", "provider"} or not select_query.startswith("SELECT "):
        raise ValueError("invalid chunked JSON query")
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
