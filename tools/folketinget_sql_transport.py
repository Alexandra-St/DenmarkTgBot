"""Shared, pure SQL Server query helpers for the Folketinget PoCs.

This module intentionally imports neither the backup validator nor the SQL
provider, so both can use the same bounded transport without an import cycle.
"""

from __future__ import annotations

import re

MANIFEST_BASE64_CHUNK_SIZE = 6000
# 64 MiB UTF-8 JSON needs at most 128 MiB UTF-16LE, or 29,827 chunks.
MANIFEST_MAX_BASE64_CHUNKS = 30000
# Five sequence digits + separator + 6000 Base64 chars = at most 6006 chars.
MANIFEST_SQL_ROW_WIDTH = 6100
CHUNK_ROW_SHAPES = frozenset({
    "null_token",
    "empty_or_whitespace",
    "sqlcmd_message",
    "sql_message",
    "rows_affected",
    "numeric_only",
    "missing_separator_ascii",
    "missing_separator_non_ascii",
    "extra_separator",
    "invalid_sequence",
    "invalid_base64_shape",
    "other",
})
CHUNK_LINE_LENGTH_BUCKETS = frozenset({"0", "1-16", "17-64", "65-256", ">256"})
CHUNK_SEPARATOR_COUNT_BUCKETS = frozenset({"0", "1", ">1"})


def classify_chunk_row_structure(line: str) -> tuple[str, str, str]:
    """Classify malformed transport text without retaining or returning its contents."""
    length = len(line)
    length_bucket = (
        "0" if length == 0 else "1-16" if length <= 16 else
        "17-64" if length <= 64 else "65-256" if length <= 256 else ">256"
    )
    separator_count = line.count("|")
    separator_bucket = "0" if separator_count == 0 else "1" if separator_count == 1 else ">1"

    if line == "NULL":
        shape = "null_token"
    elif not line.strip():
        shape = "empty_or_whitespace"
    elif re.match(r"(?i)^\s*sqlcmd\s*:\s*(?:error|warning)\b", line):
        shape = "sqlcmd_message"
    elif re.match(r"(?i)^\s*msg\s+[0-9]+\b", line):
        shape = "sql_message"
    elif re.fullmatch(r"\s*\(\s*[0-9]+\s+rows?\s+affected\s*\)\s*", line, flags=re.IGNORECASE):
        shape = "rows_affected"
    elif re.fullmatch(r"\s*[0-9]+\s*", line):
        shape = "numeric_only"
    elif separator_count == 0:
        shape = "missing_separator_ascii" if line.isascii() else "missing_separator_non_ascii"
    elif separator_count > 1:
        shape = "extra_separator"
    else:
        sequence, chunk = line.split("|", 1)
        if not re.fullmatch(r"[1-9][0-9]{0,4}", sequence):
            shape = "invalid_sequence"
        elif (not chunk or len(chunk) > MANIFEST_BASE64_CHUNK_SIZE or len(chunk) % 4
              or not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", chunk)):
            shape = "invalid_base64_shape"
        else:
            shape = "other"
    return shape, length_bucket, separator_bucket


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
        f"SET {json_variable} = COALESCE(({select_query}), N'[]'); "
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
