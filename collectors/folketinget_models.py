"""Typed source records and conservative normalization for Folketinget ODA."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import re
from typing import Generic, Mapping, TypeVar

T = TypeVar("T")
_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,7}))?(Z|[+-]\d{2}:\d{2})?$"
)


@dataclass(frozen=True)
class Diagnostic:
    code: str
    entity: str
    message: str
    entity_id: int | None = None
    related_id: int | None = None


@dataclass(frozen=True)
class NormalizationResult(Generic[T]):
    record: T | None
    diagnostics: tuple[Diagnostic, ...]
    complete: bool


@dataclass(frozen=True)
class Sag:
    id: int
    nummer: str | None
    titel: str | None
    typeid: int | None
    kategoriid: int | None
    statusid: int | None
    opdateringsdato: str | None
    updated_at_utc: str | None
    retsinformationsurl: str | None
    complete: bool = True


@dataclass(frozen=True)
class Sagstrin:
    id: int
    sagid: int
    titel: str | None
    dato: str | None
    typeid: int | None
    statusid: int | None
    opdateringsdato: str | None
    updated_at_utc: str | None
    complete: bool = True


@dataclass(frozen=True)
class SagDokument:
    id: int
    sagid: int
    dokumentid: int
    bilagsnummer: str | None
    frigivelsesdato: str | None
    rolleid: int | None
    opdateringsdato: str | None
    updated_at_utc: str | None
    complete: bool = True


@dataclass(frozen=True)
class Dokument:
    id: int
    titel: str | None
    dato: str | None
    modtagelsesdato: str | None
    frigivelsesdato: str | None
    opdateringsdato: str | None
    updated_at_utc: str | None
    complete: bool = True


@dataclass(frozen=True)
class Afstemning:
    id: int
    sagstrinid: int | None
    nummer: int | None
    vedtaget: bool | None
    konklusion: str | None
    kommentar: str | None
    mødeid: int | None
    typeid: int | None
    opdateringsdato: str | None
    updated_at_utc: str | None
    complete: bool = True


@dataclass(frozen=True)
class Sagsstatus:
    id: int
    status: str | None
    opdateringsdato: str | None
    updated_at_utc: str | None
    complete: bool = True


@dataclass(frozen=True)
class CaseStatus:
    id: int | None
    name: str | None


@dataclass(frozen=True)
class CaseStage:
    id: int
    title: str | None
    date_raw: str | None
    type_id: int | None
    status_id: int | None
    source_updated_at_raw: str | None
    updated_at_utc: str | None


@dataclass(frozen=True)
class CaseDocument:
    id: int
    link_id: int
    title: str | None
    date_raw: str | None
    released_at_raw: str | None
    document_released_at_raw: str | None
    source_updated_at_raw: str | None
    updated_at_utc: str | None
    link_source_updated_at_raw: str | None
    link_updated_at_utc: str | None


@dataclass(frozen=True)
class CaseVote:
    id: int
    stage_id: int
    number: int | None
    approved: bool | None
    conclusion: str | None
    comment: str | None
    source_updated_at_raw: str | None
    updated_at_utc: str | None


@dataclass(frozen=True)
class LegislativeCase:
    external_id: str
    source: str
    number: str | None
    title: str | None
    type_id: int | None
    category_id: int | None
    status: CaseStatus
    source_updated_at_raw: str | None
    updated_at_utc: str | None
    source_url: str
    legal_url: str | None
    stages: tuple[CaseStage, ...]
    documents: tuple[CaseDocument, ...]
    votes: tuple[CaseVote, ...]
    complete: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _identifier(
    value: object,
    *,
    field: str,
    nullable: bool,
    entity: str,
    diagnostics: list[Diagnostic],
    entity_id: int | None,
) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        diagnostics.append(
            Diagnostic("invalid_identifier", entity, f"{field} must be an integer" + (" or null" if nullable else ""), entity_id)
        )
        raise _InvalidRecord
    return value


def _text(
    value: object,
    *,
    field: str,
    entity: str,
    diagnostics: list[Diagnostic],
    entity_id: int | None,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        diagnostics.append(Diagnostic("invalid_field", entity, f"{field} must be text or null", entity_id))
        return None
    return value


def _timestamp(
    value: object,
    *,
    entity: str,
    diagnostics: list[Diagnostic],
    entity_id: int | None,
) -> tuple[str | None, str | None, bool]:
    if value is None:
        diagnostics.append(Diagnostic("missing_timestamp", entity, "opdateringsdato is missing", entity_id))
        return None, None, False
    if not isinstance(value, str):
        diagnostics.append(Diagnostic("invalid_timestamp", entity, "opdateringsdato must be text", entity_id))
        return None, None, False
    match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        diagnostics.append(Diagnostic("invalid_timestamp", entity, "opdateringsdato has an unsupported format", entity_id))
        return value, None, False
    base, fraction, offset = match.groups()
    # datetime stores microseconds. Drop only the seventh digit from the
    # derived UTC value; the exact source timestamp remains in the raw field.
    candidate = base
    if fraction:
        candidate += "." + fraction[:6].ljust(6, "0")
    candidate += "+00:00" if offset == "Z" else (offset or "")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        diagnostics.append(Diagnostic("invalid_timestamp", entity, "opdateringsdato is not valid ISO 8601", entity_id))
        return value, None, False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # ODA's source timezone is unknown; keep it exactly as published.
        return value, None, True
    normalized = parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return value, normalized, True


def _source_date(
    value: object,
    *,
    field: str,
    entity: str,
    diagnostics: list[Diagnostic],
    entity_id: int | None,
) -> str | None:
    result = _text(value, field=field, entity=entity, diagnostics=diagnostics, entity_id=entity_id)
    if result is not None:
        date_diagnostics: list[Diagnostic] = []
        _timestamp(result, entity=entity, diagnostics=date_diagnostics, entity_id=entity_id)
        if date_diagnostics:
            diagnostics.append(Diagnostic("invalid_source_date", entity, f"{field} is not a valid source date/time", entity_id))
    return result


class _InvalidRecord(Exception):
    pass


def _normalize(raw: Mapping[str, object], entity: str) -> NormalizationResult[object]:
    diagnostics: list[Diagnostic] = []
    raw_id = raw.get("id")
    try:
        entity_id = _identifier(
            raw_id,
            field="id",
            nullable=False,
            entity=entity,
            diagnostics=diagnostics,
            entity_id=None,
        )
    except _InvalidRecord:
        return NormalizationResult(None, tuple(diagnostics), False)
    assert entity_id is not None
    try:
        timestamp, utc, timestamp_valid = _timestamp(
            raw.get("opdateringsdato"), entity=entity, diagnostics=diagnostics, entity_id=entity_id
        )
        if entity == "Sag":
            record = Sag(
                entity_id,
                _text(raw.get("nummer"), field="nummer", entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _text(raw.get("titel"), field="titel", entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _identifier(raw.get("typeid"), field="typeid", nullable=True, entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _identifier(raw.get("kategoriid"), field="kategoriid", nullable=True, entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _identifier(raw.get("statusid"), field="statusid", nullable=True, entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                timestamp,
                utc,
                _text(raw.get("retsinformationsurl"), field="retsinformationsurl", entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                timestamp_valid and not diagnostics,
            )
        elif entity == "Sagstrin":
            parent_id = _identifier(raw.get("sagid"), field="sagid", nullable=False, entity=entity, diagnostics=diagnostics, entity_id=entity_id)
            record = Sagstrin(
                entity_id,
                parent_id,  # type: ignore[arg-type]
                _text(raw.get("titel"), field="titel", entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _source_date(raw.get("dato"), field="dato", entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _identifier(raw.get("typeid"), field="typeid", nullable=True, entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _identifier(raw.get("statusid"), field="statusid", nullable=True, entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                timestamp,
                utc,
                timestamp_valid and not diagnostics,
            )
        elif entity == "SagDokument":
            parent_id = _identifier(raw.get("sagid"), field="sagid", nullable=False, entity=entity, diagnostics=diagnostics, entity_id=entity_id)
            document_id = _identifier(raw.get("dokumentid"), field="dokumentid", nullable=False, entity=entity, diagnostics=diagnostics, entity_id=entity_id)
            record = SagDokument(
                entity_id,
                parent_id,  # type: ignore[arg-type]
                document_id,  # type: ignore[arg-type]
                _text(raw.get("bilagsnummer"), field="bilagsnummer", entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _source_date(raw.get("frigivelsesdato"), field="frigivelsesdato", entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _identifier(raw.get("rolleid"), field="rolleid", nullable=True, entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                timestamp,
                utc,
                timestamp_valid and not diagnostics,
            )
        elif entity == "Dokument":
            record = Dokument(
                entity_id,
                _text(raw.get("titel"), field="titel", entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _source_date(raw.get("dato"), field="dato", entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _source_date(raw.get("modtagelsesdato"), field="modtagelsesdato", entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _source_date(raw.get("frigivelsesdato"), field="frigivelsesdato", entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                timestamp,
                utc,
                timestamp_valid and not diagnostics,
            )
        elif entity == "Afstemning":
            vote_number = _identifier(raw.get("nummer"), field="nummer", nullable=True, entity=entity, diagnostics=diagnostics, entity_id=entity_id)
            approved_raw = raw.get("vedtaget")
            if approved_raw is not None and not isinstance(approved_raw, bool):
                diagnostics.append(Diagnostic("invalid_field", entity, "vedtaget must be boolean or null", entity_id))
                approved_raw = None
            record = Afstemning(
                entity_id,
                _identifier(raw.get("sagstrinid"), field="sagstrinid", nullable=True, entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                vote_number,
                approved_raw,
                _text(raw.get("konklusion"), field="konklusion", entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _text(raw.get("kommentar"), field="kommentar", entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _identifier(raw.get("mødeid"), field="mødeid", nullable=True, entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                _identifier(raw.get("typeid"), field="typeid", nullable=True, entity=entity, diagnostics=diagnostics, entity_id=entity_id),
                timestamp,
                utc,
                timestamp_valid and not diagnostics,
            )
        elif entity == "Sagsstatus":
            status_text = _text(raw.get("status"), field="status", entity=entity, diagnostics=diagnostics, entity_id=entity_id)
            if status_text is None:
                diagnostics.append(Diagnostic("missing_status_text", entity, "status label is missing", entity_id))
            record = Sagsstatus(
                entity_id,
                status_text,
                timestamp,
                utc,
                timestamp_valid and not diagnostics,
            )
        else:
            raise ValueError(f"unsupported normalization entity: {entity}")
    except _InvalidRecord:
        return NormalizationResult(None, tuple(diagnostics), False)
    complete = bool(getattr(record, "complete", True)) and not diagnostics
    if diagnostics and getattr(record, "complete", False):
        record = replace(record, complete=False)
    return NormalizationResult(record, tuple(diagnostics), complete)


def normalize_sag(raw: Mapping[str, object]) -> NormalizationResult[Sag]:
    return _normalize(raw, "Sag")  # type: ignore[return-value]


def normalize_sagstrin(raw: Mapping[str, object]) -> NormalizationResult[Sagstrin]:
    return _normalize(raw, "Sagstrin")  # type: ignore[return-value]


def normalize_sagdokument(raw: Mapping[str, object]) -> NormalizationResult[SagDokument]:
    return _normalize(raw, "SagDokument")  # type: ignore[return-value]


def normalize_dokument(raw: Mapping[str, object]) -> NormalizationResult[Dokument]:
    return _normalize(raw, "Dokument")  # type: ignore[return-value]


def normalize_afstemning(raw: Mapping[str, object]) -> NormalizationResult[Afstemning]:
    return _normalize(raw, "Afstemning")  # type: ignore[return-value]


def normalize_sagsstatus(raw: Mapping[str, object]) -> NormalizationResult[Sagsstatus]:
    return _normalize(raw, "Sagsstatus")  # type: ignore[return-value]


def normalize_many(rows: object, entity: str) -> NormalizationResult[tuple[object, ...]]:
    normalizers = {
        "Sag": normalize_sag,
        "Sagstrin": normalize_sagstrin,
        "SagDokument": normalize_sagdokument,
        "Dokument": normalize_dokument,
        "Afstemning": normalize_afstemning,
        "Sagsstatus": normalize_sagsstatus,
    }
    if entity not in normalizers:
        raise ValueError("unsupported normalization entity")
    if not isinstance(rows, (list, tuple)):
        diagnostic = Diagnostic("invalid_response", entity, "records must be a list")
        return NormalizationResult((), (diagnostic,), False)
    records: list[object] = []
    diagnostics: list[Diagnostic] = []
    complete = True
    normalizer = normalizers[entity]
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            diagnostics.append(Diagnostic("invalid_record", entity, f"record at index {index} is not an object"))
            complete = False
            continue
        result = normalizer(row)
        diagnostics.extend(result.diagnostics)
        if result.record is not None:
            records.append(result.record)
        if not result.complete:
            complete = False
    return NormalizationResult(tuple(records), tuple(diagnostics), complete)


__all__ = [
    "Afstemning", "CaseDocument", "CaseStage", "CaseStatus", "CaseVote", "Diagnostic",
    "Dokument", "LegislativeCase", "NormalizationResult", "Sag", "SagDokument",
    "Sagstrin", "Sagsstatus", "normalize_afstemning", "normalize_dokument", "normalize_many",
    "normalize_sag", "normalize_sagdokument", "normalize_sagstrin", "normalize_sagsstatus",
]
