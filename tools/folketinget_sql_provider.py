"""Read-only SQL Server provider for Folketinget candidate extraction.

The caller supplies only the database password and a query executor that
returns the decoded JSON string from the shared strict chunk transport.
No caller-controlled SQL fragments are accepted.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Callable, Mapping, Sequence

TOOLS_DIRECTORY = str(Path(__file__).resolve().parent)
if TOOLS_DIRECTORY not in sys.path:
    sys.path.insert(0, TOOLS_DIRECTORY)

from folketinget_sql_transport import (
    CHUNK_LINE_LENGTH_BUCKETS,
    CHUNK_ROW_SHAPES,
    CHUNK_SEPARATOR_COUNT_BUCKETS,
    build_chunked_json_query,
    quote_identifier,
)


REQUIRED_ENTITIES = ("Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus")
PROJECTIONS: dict[str, tuple[str, ...]] = {
    "Sag": ("id", "nummer", "titel", "typeid", "kategoriid", "statusid", "opdateringsdato", "retsinformationsurl"),
    "Sagstrin": ("id", "sagid", "titel", "dato", "typeid", "statusid", "opdateringsdato"),
    "SagDokument": ("id", "sagid", "dokumentid", "bilagsnummer", "frigivelsesdato", "rolleid", "opdateringsdato"),
    "Dokument": ("id", "titel", "dato", "modtagelsesdato", "frigivelsesdato", "opdateringsdato"),
    "Afstemning": ("id", "sagstrinid", "nummer", "vedtaget", "konklusion", "kommentar", "mødeid", "typeid", "opdateringsdato"),
    "Sagsstatus": ("id", "status", "opdateringsdato"),
}
RELATION_FIELDS: dict[str, frozenset[str]] = {
    "Sag": frozenset({"statusid"}),
    "Sagstrin": frozenset({"sagid"}),
    "SagDokument": frozenset({"sagid", "dokumentid"}),
    "Dokument": frozenset(),
    "Afstemning": frozenset({"sagstrinid"}),
    "Sagsstatus": frozenset(),
}
MAX_IDS_PER_QUERY = 500
MAX_LOOKUP_VALUES = 100_000
SQL_INT_MIN = -(2**63)
SQL_INT_MAX = 2**63 - 1
SAFE_PROVIDER_ERROR_CATEGORIES = frozenset({
    "provider_configuration_invalid",
    "provider_entity_not_allowed",
    "provider_values_invalid",
    "provider_field_not_allowed",
    "provider_query_invalid",
    "provider_query_failed",
    "provider_transport_failed",
    "provider_json_invalid",
    "provider_row_shape_invalid",
    "provider_id_type_invalid",
    "provider_relation_field_type_invalid",
    "provider_unexpected_row",
    "provider_duplicate_conflict",
})
SAFE_TRANSPORT_CATEGORIES = frozenset({
    "manifest_page_invalid",
    "manifest_page_chunk_sequence_invalid",
    "manifest_page_chunk_row_invalid",
    "manifest_page_base64_format_invalid",
    "manifest_page_base64_decode_failed",
    "manifest_page_utf16_decode_failed",
    "manifest_page_json_invalid",
    "manifest_page_too_large",
    "manifest_page_sqlcmd_failed",
})
ADAPTIVE_RETRY_TRANSPORT_CATEGORIES = frozenset({
    "manifest_page_chunk_row_invalid",
    "manifest_page_chunk_sequence_invalid",
    "manifest_page_too_large",
})
MAX_ADAPTIVE_SPLIT_DEPTH = 9  # ceil(log2(MAX_IDS_PER_QUERY))


class SQLProviderError(Exception):
    """Safe provider failure category; contains no SQL or row data."""

    def __init__(
        self,
        category: str,
        *,
        transport_category: str | None = None,
        transport_row_shape: str | None = None,
        transport_line_length_bucket: str | None = None,
        transport_separator_count_bucket: str | None = None,
    ) -> None:
        self.category = category
        self.transport_category = transport_category
        self.transport_row_shape = transport_row_shape if transport_row_shape in CHUNK_ROW_SHAPES else None
        self.transport_line_length_bucket = (
            transport_line_length_bucket if transport_line_length_bucket in CHUNK_LINE_LENGTH_BUCKETS else None
        )
        self.transport_separator_count_bucket = (
            transport_separator_count_bucket
            if transport_separator_count_bucket in CHUNK_SEPARATOR_COUNT_BUCKETS else None
        )
        super().__init__(category)


QueryExecutor = Callable[[str, str], str]


class SQLServerFolketingetProvider:
    """A SELECT-only adapter over the temporary restored ODA database."""

    def __init__(
        self,
        schemas: Mapping[str, str | tuple[str, str]],
        query_executor: QueryExecutor,
        column_types: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        if not callable(query_executor) or set(schemas) != set(REQUIRED_ENTITIES):
            raise SQLProviderError("provider_configuration_invalid")
        validated: dict[str, str] = {}
        for entity in REQUIRED_ENTITIES:
            item = schemas[entity]
            schema, object_type = item if isinstance(item, tuple) and len(item) == 2 else (item, "USER_TABLE")
            if object_type != "USER_TABLE" or not isinstance(schema, str) or not schema or len(schema) > 128 or "\x00" in schema:
                raise SQLProviderError("provider_configuration_invalid")
            validated[entity] = schema
        self._schemas = validated
        self._query_executor = query_executor
        self._adaptive_split_count = 0
        self._adaptive_split_success_count = 0
        self._adaptive_split_max_depth = 0
        self._adaptive_terminal_failure_count = 0
        self._adaptive_terminal_row_shapes: Counter[str] = Counter()
        self._adaptive_terminal_line_lengths: Counter[str] = Counter()
        self._column_types: dict[str, dict[str, str]] = {entity: {} for entity in REQUIRED_ENTITIES}
        if column_types is not None:
            if set(column_types) != set(REQUIRED_ENTITIES):
                raise SQLProviderError("provider_configuration_invalid")
            for entity, fields in column_types.items():
                if set(fields) - set(PROJECTIONS[entity]) or any(not isinstance(value, str) for value in fields.values()):
                    raise SQLProviderError("provider_configuration_invalid")
                self._column_types[entity] = {field: value.casefold() for field, value in fields.items()}

    @staticmethod
    def _entity(entity: str) -> None:
        if entity not in PROJECTIONS:
            raise SQLProviderError("provider_entity_not_allowed")

    @staticmethod
    def _values(values: Sequence[int]) -> tuple[int, ...]:
        if isinstance(values, (str, bytes)):
            raise SQLProviderError("provider_values_invalid")
        try:
            selected = tuple(values)
        except TypeError:
            raise SQLProviderError("provider_values_invalid") from None
        if len(selected) > MAX_LOOKUP_VALUES or any(
            isinstance(value, bool) or not isinstance(value, int) or value < SQL_INT_MIN or value > SQL_INT_MAX
            for value in selected
        ):
            raise SQLProviderError("provider_values_invalid")
        return tuple(sorted(set(selected)))

    def _field(self, entity: str, field: str) -> None:
        self._entity(entity)
        if field != "id" and field not in RELATION_FIELDS[entity]:
            raise SQLProviderError("provider_field_not_allowed")

    def _query(self, entity: str, field: str, values: tuple[int, ...]) -> tuple[Mapping[str, object], ...]:
        self._field(entity, field)
        if not values:
            return ()
        try:
            expressions = []
            for name in PROJECTIONS[entity]:
                identifier = quote_identifier(name)
                sql_type = self._column_types[entity].get(name)
                if sql_type == "text":
                    expression = f"CONVERT(varchar(max), {identifier})"
                elif sql_type == "ntext":
                    expression = f"CONVERT(nvarchar(max), {identifier})"
                else:
                    expression = identifier
                expressions.append(f"{expression} AS {quote_identifier(name)}")
            projection = ", ".join(expressions)
            numeric_values = ",".join(str(value) for value in values)
            table = f"{quote_identifier(self._schemas[entity])}.{quote_identifier(entity)}"
            select = (
                f"SELECT {projection} FROM {table} WHERE {quote_identifier(field)} IN ({numeric_values}) "
                f"ORDER BY {quote_identifier('id')} FOR JSON PATH, INCLUDE_NULL_VALUES"
            )
            query = build_chunked_json_query(select, variable="provider")
        except ValueError:
            raise SQLProviderError("provider_query_invalid") from None
        try:
            payload = self._query_executor(query, entity)
        except SQLProviderError:
            raise
        except Exception as exc:
            transport_category = getattr(exc, "category", None)
            if (
                isinstance(transport_category, str)
                and (
                    transport_category in SAFE_TRANSPORT_CATEGORIES
                    or re.fullmatch(r"manifest_page_sql_msg_[0-9]{1,6}", transport_category)
                )
            ):
                raise SQLProviderError(
                    "provider_transport_failed",
                    transport_category=transport_category,
                    transport_row_shape=getattr(exc, "row_shape_category", None),
                    transport_line_length_bucket=getattr(exc, "line_length_bucket", None),
                    transport_separator_count_bucket=getattr(exc, "separator_count_bucket", None),
                ) from None
            raise SQLProviderError("provider_query_failed") from None
        rows = self._parse_rows(payload, entity)
        requested = set(values)
        if any(row.get(field) not in requested for row in rows):
            raise SQLProviderError("provider_unexpected_row")
        return rows

    @staticmethod
    def _parse_rows(payload: str, entity: str) -> tuple[Mapping[str, object], ...]:
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise SQLProviderError("provider_json_invalid") from None
        if not isinstance(decoded, list):
            raise SQLProviderError("provider_json_invalid")
        expected = set(PROJECTIONS[entity])
        rows: list[Mapping[str, object]] = []
        for row in decoded:
            if not isinstance(row, dict) or set(row) != expected:
                raise SQLProviderError("provider_row_shape_invalid")
            identifier = row.get("id")
            if isinstance(identifier, bool) or not isinstance(identifier, int) or not SQL_INT_MIN <= identifier <= SQL_INT_MAX:
                raise SQLProviderError("provider_id_type_invalid")
            for field in RELATION_FIELDS[entity]:
                value = row.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                    raise SQLProviderError("provider_relation_field_type_invalid")
            rows.append(row)
        return tuple(rows)

    @staticmethod
    def _merge(rows: Sequence[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
        selected: dict[int, Mapping[str, object]] = {}
        for row in rows:
            identifier = row["id"]
            previous = selected.get(identifier)  # type: ignore[arg-type]
            if previous is not None:
                if json.dumps(previous, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != json.dumps(
                    row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ):
                    raise SQLProviderError("provider_duplicate_conflict")
                continue
            selected[identifier] = row  # type: ignore[index]
        return tuple(selected[key] for key in sorted(selected))

    def _query_with_adaptive_split(
        self, entity: str, field: str, values: tuple[int, ...], *, depth: int = 0
    ) -> tuple[Mapping[str, object], ...]:
        """Retry only selected read-transport failures by deterministic bisection."""
        try:
            return self._query(entity, field, values)
        except SQLProviderError as error:
            retryable = (
                error.category == "provider_transport_failed"
                and error.transport_category in ADAPTIVE_RETRY_TRANSPORT_CATEGORIES
            )
            if retryable and len(values) == 1:
                self._record_adaptive_terminal_failure(error)
                raise
            if (not retryable or len(values) <= 1 or depth >= MAX_ADAPTIVE_SPLIT_DEPTH):
                raise
            midpoint = len(values) // 2
            left, right = values[:midpoint], values[midpoint:]
            # Defensive invariant: recursive calls always strictly reduce the input.
            if not left or not right or len(left) >= len(values) or len(right) >= len(values):
                raise
            self._adaptive_split_count += 1
            self._adaptive_split_max_depth = max(self._adaptive_split_max_depth, depth + 1)
            left_rows = self._query_with_adaptive_split(entity, field, left, depth=depth + 1)
            right_rows = self._query_with_adaptive_split(entity, field, right, depth=depth + 1)
            merged = self._merge((*left_rows, *right_rows))
            # Count a success only when the original bounded lookup was fully recovered.
            if depth == 0:
                self._adaptive_split_success_count += 1
            return merged

    def _record_adaptive_terminal_failure(self, error: SQLProviderError) -> None:
        self._adaptive_terminal_failure_count += 1
        row_shape = error.transport_row_shape
        self._adaptive_terminal_row_shapes[row_shape if row_shape in CHUNK_ROW_SHAPES else "other"] += 1
        line_bucket = error.transport_line_length_bucket
        if line_bucket in CHUNK_LINE_LENGTH_BUCKETS:
            self._adaptive_terminal_line_lengths[line_bucket] += 1

    def adaptive_summary(self) -> dict[str, object]:
        """Return aggregate-only adaptive query metrics; never includes lookup values."""
        return {
            "adaptive_split_count": self._adaptive_split_count,
            "adaptive_split_success_count": self._adaptive_split_success_count,
            "adaptive_split_max_depth": self._adaptive_split_max_depth,
            "adaptive_terminal_failure_count": self._adaptive_terminal_failure_count,
            "adaptive_terminal_failures_by_row_shape": dict(sorted(self._adaptive_terminal_row_shapes.items())),
            "adaptive_terminal_failures_by_line_length_bucket": dict(sorted(self._adaptive_terminal_line_lengths.items())),
        }

    def _lookup(self, entity: str, field: str, values: Sequence[int]) -> tuple[Mapping[str, object], ...]:
        self._field(entity, field)
        selected = self._values(values)
        merged: list[Mapping[str, object]] = []
        for offset in range(0, len(selected), MAX_IDS_PER_QUERY):
            merged.extend(self._query_with_adaptive_split(entity, field, selected[offset : offset + MAX_IDS_PER_QUERY]))
        return self._merge(merged)

    def get_by_ids(self, entity: str, ids: Sequence[int]) -> tuple[Mapping[str, object], ...]:
        return self._lookup(entity, "id", ids)

    def find_by_field(self, entity: str, field: str, values: Sequence[int]) -> tuple[Mapping[str, object], ...]:
        if field not in RELATION_FIELDS.get(entity, frozenset()):
            raise SQLProviderError("provider_field_not_allowed")
        return self._lookup(entity, field, values)
