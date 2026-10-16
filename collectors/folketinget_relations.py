"""ID-based Folketinget case assembly and bounded targeted hydration."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from typing import Iterable, Mapping, Sequence, TypeVar

from collectors.folketinget import ODA_ROOT, ODataClient, ODataError, ODataHTTPError, ODataPageError, ODataPaginationError, ScanResult, build_url
from collectors.folketinget_models import (
    Afstemning,
    CaseDocument,
    CaseStage,
    CaseStatus,
    CaseVote,
    Diagnostic,
    Dokument,
    LegislativeCase,
    NormalizationResult,
    Sag,
    SagDokument,
    Sagstrin,
    Sagsstatus,
    normalize_many,
)


@dataclass(frozen=True)
class AssemblyResult:
    cases: tuple[LegislativeCase, ...]
    diagnostics: tuple[Diagnostic, ...]
    complete: bool


@dataclass(frozen=True)
class HydrationResult:
    case: LegislativeCase | None
    diagnostics: tuple[Diagnostic, ...]
    complete: bool


_Model = TypeVar("_Model")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BulkHydrationResult:
    cases: tuple[LegislativeCase, ...]
    diagnostics: tuple[Diagnostic, ...]
    complete: bool


def _record_key(record: object) -> int:
    return int(getattr(record, "id"))


def _canonical(record: object) -> str:
    return json.dumps(asdict(record), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _deduplicate(records: Iterable[_Model], entity: str) -> tuple[dict[int, _Model], list[Diagnostic]]:
    selected: dict[int, _Model] = {}
    diagnostics: list[Diagnostic] = []
    for record in records:
        record_id = _record_key(record)
        previous = selected.get(record_id)
        if previous is None:
            selected[record_id] = record
            continue
        if _canonical(previous) == _canonical(record):
            continue
        # Stable tie-break: choose the lexicographically smaller canonical model,
        # and always report that conflicting source rows shared an official ID.
        chosen = min((previous, record), key=_canonical)
        selected[record_id] = chosen
        diagnostics.append(
            Diagnostic("duplicate_conflict", entity, "conflicting records share an official ID", record_id)
        )
    diagnostics.sort(key=lambda item: (item.entity, item.entity_id or -1, item.code, item.message))
    return selected, diagnostics


def _diag(code: str, entity: str, message: str, *, entity_id: int | None = None, related_id: int | None = None) -> Diagnostic:
    return Diagnostic(code, entity, message, entity_id, related_id)


def assemble_cases(
    sags: Sequence[Sag],
    *,
    stages: Sequence[Sagstrin] = (),
    document_links: Sequence[SagDokument] = (),
    documents: Sequence[Dokument] = (),
    votes: Sequence[Afstemning] = (),
    statuses: Sequence[Sagsstatus] = (),
    diagnostics: Sequence[Diagnostic] = (),
) -> AssemblyResult:
    """Assemble source models only through official numeric IDs."""
    all_diagnostics = list(diagnostics)
    sags_by_id, issues = _deduplicate(sags, "Sag")
    all_diagnostics.extend(issues)
    stages_by_id, issues = _deduplicate(stages, "Sagstrin")
    all_diagnostics.extend(issues)
    links_by_id, issues = _deduplicate(document_links, "SagDokument")
    all_diagnostics.extend(issues)
    documents_by_id, issues = _deduplicate(documents, "Dokument")
    all_diagnostics.extend(issues)
    votes_by_id, issues = _deduplicate(votes, "Afstemning")
    all_diagnostics.extend(issues)
    statuses_by_id, issues = _deduplicate(statuses, "Sagsstatus")
    all_diagnostics.extend(issues)

    stages_for_case: dict[int, list[Sagstrin]] = {case_id: [] for case_id in sags_by_id}
    for stage in stages_by_id.values():
        if stage.sagid not in sags_by_id:
            all_diagnostics.append(_diag("orphan_stage", "Sagstrin", "stage references a missing case", entity_id=stage.id, related_id=stage.sagid))
            continue
        stages_for_case[stage.sagid].append(stage)

    links_for_case: dict[int, list[SagDokument]] = {case_id: [] for case_id in sags_by_id}
    for link in links_by_id.values():
        if link.sagid not in sags_by_id:
            all_diagnostics.append(_diag("orphan_document_link", "SagDokument", "document link references a missing case", entity_id=link.id, related_id=link.sagid))
            continue
        links_for_case[link.sagid].append(link)

    votes_for_case: dict[int, list[Afstemning]] = {case_id: [] for case_id in sags_by_id}
    for vote in votes_by_id.values():
        if vote.sagstrinid is None:
            all_diagnostics.append(_diag("unresolved_vote", "Afstemning", "vote has null sagstrinid and cannot be assigned to a case", entity_id=vote.id))
            continue
        stage = stages_by_id.get(vote.sagstrinid)
        if stage is None:
            all_diagnostics.append(_diag("unresolved_vote", "Afstemning", "vote references a missing stage", entity_id=vote.id, related_id=vote.sagstrinid))
            continue
        if stage.sagid not in sags_by_id:
            all_diagnostics.append(_diag("unresolved_vote", "Afstemning", "vote stage has no loaded case", entity_id=vote.id, related_id=stage.sagid))
            continue
        votes_for_case[stage.sagid].append(vote)

    linked_document_ids = {link.dokumentid for link in links_by_id.values()}
    for document_id in documents_by_id:
        if document_id not in linked_document_ids:
            all_diagnostics.append(_diag("orphan_document", "Dokument", "document has no loaded SagDokument link", entity_id=document_id))

    cases: list[LegislativeCase] = []
    for case_id, sag in sorted(sags_by_id.items()):
        case_links = sorted(links_for_case[case_id], key=lambda item: (item.id, item.dokumentid))
        case_stages = sorted(stages_for_case[case_id], key=lambda item: item.id)
        case_votes = sorted(votes_for_case[case_id], key=lambda item: item.id)
        status_name: str | None = None
        status_complete = True
        if sag.statusid is not None:
            status = statuses_by_id.get(sag.statusid)
            if status is None:
                status_complete = False
                already_failed = any(
                    item.code == "status_fetch_failed" and item.entity_id == sag.id
                    for item in all_diagnostics
                )
                if not already_failed:
                    all_diagnostics.append(_diag("missing_status", "Sagsstatus", "case status reference was not resolved", entity_id=sag.id, related_id=sag.statusid))
            else:
                status_name = status.status
                status_complete = status.complete
        stage_models = tuple(
            CaseStage(item.id, item.titel, item.dato, item.typeid, item.statusid, item.opdateringsdato, item.updated_at_utc)
            for item in case_stages
        )
        document_models: list[CaseDocument] = []
        document_complete = True
        for link in case_links:
            document = documents_by_id.get(link.dokumentid)
            if document is None:
                document_complete = False
                lookup_failed = any(
                    item.entity == "Dokument" and item.code in {"related_page_failed", "related_hydration_failed"}
                    for item in all_diagnostics
                )
                if not lookup_failed:
                    all_diagnostics.append(_diag("missing_document", "Dokument", "document link target was not resolved", entity_id=link.id, related_id=link.dokumentid))
                document_models.append(
                    CaseDocument(
                        link.dokumentid, link.id, None, None, link.frigivelsesdato,
                        None, None, None, link.opdateringsdato, link.updated_at_utc,
                    )
                )
            else:
                document_models.append(
                    CaseDocument(
                        document.id, link.id, document.titel, document.dato,
                        link.frigivelsesdato, document.frigivelsesdato,
                        document.opdateringsdato, document.updated_at_utc,
                        link.opdateringsdato, link.updated_at_utc,
                    )
                )
                document_complete = document_complete and document.complete and link.complete
        vote_models = tuple(
            CaseVote(
                item.id, item.sagstrinid, item.nummer, item.vedtaget, item.konklusion,
                item.kommentar, item.opdateringsdato, item.updated_at_utc,
            )
            for item in case_votes
            if item.sagstrinid is not None
        )
        related_complete = all(item.complete for item in case_stages) and all(item.complete for item in case_votes)
        related_complete = related_complete and all(item.complete for item in case_links)
        complete = sag.complete and status_complete and document_complete and related_complete
        cases.append(
            LegislativeCase(
                external_id=str(sag.id),
                source="folketinget",
                number=sag.nummer,
                title=sag.titel,
                type_id=sag.typeid,
                category_id=sag.kategoriid,
                status=CaseStatus(sag.statusid, status_name),
                source_updated_at_raw=sag.opdateringsdato,
                updated_at_utc=sag.updated_at_utc,
                source_url=f"{ODA_ROOT}Sag({sag.id})",
                legal_url=sag.retsinformationsurl,
                stages=stage_models,
                documents=tuple(sorted(document_models, key=lambda item: (item.link_id, item.id))),
                votes=vote_models,
                complete=complete,
            )
        )

    # Relate diagnosable source conflicts and validation failures back to cases.
    # Global orphan diagnostics remain visible without falsely poisoning every case.
    completed_cases: list[LegislativeCase] = []
    for case in cases:
        case_id = int(case.external_id)
        stage_ids = {item.id for item in stages_for_case[case_id]}
        link_ids = {item.id for item in links_for_case[case_id]}
        doc_ids = {item.dokumentid for item in links_for_case[case_id]}
        vote_ids = {item.id for item in votes_for_case[case_id]}
        status_id = sags_by_id[case_id].statusid
        has_case_issue = any(
            (item.entity == "Sag" and item.entity_id == case_id)
            or (item.entity == "Sagstrin" and item.entity_id in stage_ids)
            or (item.entity == "SagDokument" and item.entity_id in link_ids)
            or (item.entity == "Dokument" and (item.entity_id in doc_ids or item.related_id in doc_ids))
            or (item.entity == "Afstemning" and item.entity_id in vote_ids)
            or (item.entity == "Sagsstatus" and (item.entity_id == status_id or item.related_id == status_id))
            or (item.code in {"related_page_failed", "related_hydration_failed", "status_fetch_failed"} and item.entity_id == case_id)
            for item in all_diagnostics
        )
        completed_cases.append(replace(case, complete=case.complete and not has_case_issue))
    cases = completed_cases

    all_diagnostics.sort(key=lambda item: (item.entity, item.entity_id or -1, item.code, item.related_id or -1, item.message))
    return AssemblyResult(tuple(cases), tuple(all_diagnostics), all(case.complete for case in cases) and not all_diagnostics)


def _as_batch(rows: object, entity: str) -> NormalizationResult[tuple[object, ...]]:
    return normalize_many(rows, entity)


def _safe_scan(client: ODataClient, collection: str, expression: str, case_id: int) -> tuple[list[dict[str, object]], bool, list[Diagnostic]]:
    try:
        result = client.scan(collection, filter=expression)
        return list(result.records), result.metadata.complete, []
    except ODataPageError as exc:
        return list(exc.partial_result.records), False, [
            _diag(
                "related_page_failed", collection,
                f"page {exc.page_number} failed ({type(exc.cause).__name__ if exc.cause else 'unknown error'})",
                entity_id=case_id,
            )
        ]
    except ODataError as exc:
        return [], False, [_diag("related_hydration_failed", collection, f"related scan failed ({type(exc).__name__})", entity_id=case_id)]


def _id_filter(field: str, values: Sequence[int]) -> str:
    return " or ".join(f"{field} eq {value}" for value in values)


def _chunks(values: Sequence[int], size: int = 50) -> Iterable[Sequence[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


_PROJECTIONS = {
    "Sag": "id,nummer,titel,statusid,typeid,kategoriid,opdateringsdato,retsinformationsurl",
    "Sagstrin": "id,sagid,titel,dato,typeid,statusid,opdateringsdato",
    "SagDokument": "id,sagid,dokumentid,bilagsnummer,frigivelsesdato,rolleid,opdateringsdato",
    "Dokument": "id,titel,dato,modtagelsesdato,frigivelsesdato,opdateringsdato",
    "Afstemning": "id,sagstrinid,nummer,vedtaget,konklusion,kommentar,mødeid,typeid,opdateringsdato",
    "Sagsstatus": "id,status,opdateringsdato",
}
_MAX_ADAPTIVE_GROUP_SIZE = 8


def _page_error_category(error: ODataPageError) -> str:
    cause = error.cause
    if isinstance(cause, ODataHTTPError) and cause.status_code is not None:
        category = f"HTTP_{cause.status_code}"
    elif isinstance(cause, ODataPaginationError):
        detail = str(cause).lower()
        if "cursor did not advance" in detail:
            category = "cursor_not_advanced"
        elif "exceeded the captured upper bound" in detail:
            category = "above_captured_upper_bound"
        elif "page limit" in detail:
            category = "page_limit"
        else:
            category = "pagination_error"
    else:
        category = type(cause).__name__ if cause else "page_error"
    return f"{category}:page={error.page_number}:url_chars={error.request_url_chars or 'unknown'}"


def _groups(mapping: Mapping[int, set[int]], size: int = 40) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    ids = sorted(mapping)
    return [
        (tuple(batch), tuple(sorted({case_id for item in batch for case_id in mapping[item]})))
        for batch in _chunks(ids, size)
    ]


def _bulk_scan_groups(
    client: ODataClient,
    collection: str,
    field: str,
    mapping: Mapping[int, set[int]],
    *,
    workers: int = 4,
    batch_size: int = 40,
) -> tuple[list[dict[str, object]], list[Diagnostic], bool]:
    groups = _groups(mapping, batch_size)
    if not groups:
        return [], [], True

    def fetch_smaller(ids: tuple[int, ...]) -> tuple[list[dict[str, object]], list[tuple[tuple[int, ...], str]]]:
        id_mapping = {item: mapping[item] for item in ids}
        rows: list[dict[str, object]] = []
        failures: list[tuple[tuple[int, ...], str]] = []
        for child in _groups(id_mapping, _MAX_ADAPTIVE_GROUP_SIZE):
            child_rows, child_failures = fetch(child)
            rows.extend(child_rows)
            failures.extend(child_failures)
        return rows, failures

    def fetch(group: tuple[tuple[int, ...], tuple[int, ...]]) -> tuple[list[dict[str, object]], list[tuple[tuple[int, ...], str]]]:
        ids, case_ids = group
        expression = " or ".join(f"{field} eq {item}" for item in ids)
        url_length = len(build_url(collection, {
            "$filter": f"({expression})",
            "$select": "id,opdateringsdato",
            "$orderby": "opdateringsdato desc,id desc",
            "$top": 1,
        }))
        try:
            result = client.scan(collection, filter=f"({expression})", select=_PROJECTIONS[collection], max_pages=100)
            LOGGER.info("ODA bulk group entity=%s ids=%d url_chars=%d status=%s", collection, len(ids), url_length, "complete" if result.metadata.complete else "incomplete")
            return list(result.records), [] if result.metadata.complete else [(case_ids, "incomplete_scan")]
        except ODataHTTPError as exc:
            status = exc.status_code
            if status == 400 and len(ids) > _MAX_ADAPTIVE_GROUP_SIZE:
                LOGGER.warning("ODA bulk group entity=%s ids=%d url_chars=%d status=400 action=split", collection, len(ids), url_length)
                return fetch_smaller(ids)
            category = f"HTTP_{status}" if status is not None else "HTTP_error"
            LOGGER.warning("ODA bulk group entity=%s ids=%d url_chars=%d status=%s action=fail", collection, len(ids), url_length, category)
            return [], [(case_ids, category)]
        except ODataPageError as exc:
            if isinstance(exc.cause, ODataHTTPError) and exc.cause.status_code == 400 and len(ids) > _MAX_ADAPTIVE_GROUP_SIZE:
                LOGGER.warning("ODA bulk group entity=%s ids=%d url_chars=%d status=400 action=split", collection, len(ids), url_length)
                return fetch_smaller(ids)
            category = _page_error_category(exc)
            failing_url_chars = exc.request_url_chars or url_length
            LOGGER.warning("ODA bulk group entity=%s ids=%d url_chars=%d status=%s action=fail", collection, len(ids), failing_url_chars, category)
            return list(exc.partial_result.records), [(case_ids, category)]
        except ODataError as exc:
            category = type(exc).__name__
            LOGGER.warning("ODA bulk group entity=%s ids=%d url_chars=%d status=%s action=fail", collection, len(ids), url_length, category)
            return [], [(case_ids, category)]

    with ThreadPoolExecutor(max_workers=min(workers, len(groups))) as pool:
        results = list(pool.map(fetch, groups))
    rows: list[dict[str, object]] = []
    diagnostics: list[Diagnostic] = []
    complete = True
    for group_rows, failed_groups in results:
        rows.extend(group_rows)
        complete = complete and not failed_groups
        for case_ids, error in failed_groups:
            for case_id in case_ids:
                diagnostics.append(_diag(
                    "related_page_failed", collection,
                    f"bulk relationship lookup failed ({error or 'incomplete_scan'})",
                    entity_id=case_id,
                ))
    LOGGER.info(
        "ODA relationship progress entity=%s groups=%d records=%d complete=%s",
        collection, len(groups), len(rows), str(complete).lower(),
    )
    return rows, diagnostics, complete


def load_cases(
    client: ODataClient,
    case_ids: Sequence[int],
    *,
    workers: int = 4,
    batch_size: int = 40,
) -> BulkHydrationResult:
    """Hydrate many cases in bounded ID groups, avoiding per-case ODA requests."""
    ids = sorted(set(case_ids))
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in ids):
        raise ValueError("case_ids must contain positive integers")
    if workers < 1 or workers > 6 or batch_size < 1 or batch_size > 100:
        raise ValueError("invalid bulk hydration limits")
    if not ids:
        return BulkHydrationResult((), (), True)

    diagnostics: list[Diagnostic] = []
    complete = True
    case_mapping = {item: {item} for item in ids}
    raw_sags, found, ok = _bulk_scan_groups(client, "Sag", "id", case_mapping, workers=workers, batch_size=batch_size)
    diagnostics.extend(found)
    complete = complete and ok
    sag_batch = _as_batch(raw_sags, "Sag")
    diagnostics.extend(sag_batch.diagnostics)
    sags = [item for item in (sag_batch.record or ()) if isinstance(item, Sag) and item.id in case_mapping]
    sag_ids = {item.id for item in sags}
    for missing_id in sorted(set(ids) - sag_ids):
        diagnostics.append(_diag("case_not_found", "Sag", "requested case was not returned", entity_id=missing_id))
        complete = False

    stages_raw, stages_diags, stages_ok = _bulk_scan_groups(
        client, "Sagstrin", "sagid", case_mapping, workers=workers, batch_size=batch_size,
    )
    links_raw, links_diags, links_ok = _bulk_scan_groups(
        client, "SagDokument", "sagid", case_mapping, workers=workers, batch_size=batch_size,
    )
    diagnostics.extend(stages_diags)
    diagnostics.extend(links_diags)
    complete = complete and stages_ok and links_ok
    stages_batch = _as_batch(stages_raw, "Sagstrin")
    links_batch = _as_batch(links_raw, "SagDokument")
    diagnostics.extend(stages_batch.diagnostics)
    diagnostics.extend(links_batch.diagnostics)
    stages = [item for item in (stages_batch.record or ()) if isinstance(item, Sagstrin)]
    links = [item for item in (links_batch.record or ()) if isinstance(item, SagDokument)]

    document_mapping: dict[int, set[int]] = {}
    for item in links:
        document_mapping.setdefault(item.dokumentid, set()).add(item.sagid)
    stage_mapping: dict[int, set[int]] = {}
    for item in stages:
        stage_mapping.setdefault(item.id, set()).add(item.sagid)
    status_mapping: dict[int, set[int]] = {}
    for item in sags:
        if item.statusid is not None:
            status_mapping.setdefault(item.statusid, set()).add(item.id)

    docs_raw, docs_diags, docs_ok = _bulk_scan_groups(
        client, "Dokument", "id", document_mapping, workers=workers, batch_size=batch_size,
    )
    votes_raw, votes_diags, votes_ok = _bulk_scan_groups(
        client, "Afstemning", "sagstrinid", stage_mapping, workers=workers, batch_size=batch_size,
    )
    statuses_raw, statuses_diags, statuses_ok = _bulk_scan_groups(
        client, "Sagsstatus", "id", status_mapping, workers=workers, batch_size=batch_size,
    )
    diagnostics.extend(docs_diags)
    diagnostics.extend(votes_diags)
    diagnostics.extend(statuses_diags)
    complete = complete and docs_ok and votes_ok and statuses_ok
    docs_batch = _as_batch(docs_raw, "Dokument")
    votes_batch = _as_batch(votes_raw, "Afstemning")
    statuses_batch = _as_batch(statuses_raw, "Sagsstatus")
    diagnostics.extend(docs_batch.diagnostics)
    diagnostics.extend(votes_batch.diagnostics)
    diagnostics.extend(statuses_batch.diagnostics)
    documents = [item for item in (docs_batch.record or ()) if isinstance(item, Dokument)]
    votes = [item for item in (votes_batch.record or ()) if isinstance(item, Afstemning)]
    statuses = [item for item in (statuses_batch.record or ()) if isinstance(item, Sagsstatus)]

    assembled = assemble_cases(
        sags, stages=stages, document_links=links, documents=documents,
        votes=votes, statuses=statuses, diagnostics=diagnostics,
    )
    by_id = {int(item.external_id): item for item in assembled.cases}
    missing = sorted(set(ids) - set(by_id))
    results = tuple(by_id[item] for item in sorted(by_id))
    complete = complete and not missing and all(item.complete for item in results)
    LOGGER.info(
        "ODA hydration progress cases_requested=%d cases_returned=%d complete=%s",
        len(ids), len(results), str(complete).lower(),
    )
    return BulkHydrationResult(results, assembled.diagnostics, complete)


def load_case(client: ODataClient, case_id: int) -> HydrationResult:
    """Fetch one case and its related rows using bounded, ID-filtered requests."""
    if isinstance(case_id, bool) or not isinstance(case_id, int) or case_id <= 0:
        raise ValueError("case_id must be a positive integer")
    diagnostics: list[Diagnostic] = []
    try:
        sag_rows = client.fetch_page("Sag", {"$filter": f"id eq {case_id}", "$top": 2})
    except ODataError:
        diagnostic = _diag("case_fetch_failed", "Sag", "case request failed", entity_id=case_id)
        return HydrationResult(None, (diagnostic,), False)
    sag_batch = _as_batch(sag_rows, "Sag")
    diagnostics.extend(sag_batch.diagnostics)
    sag_records = sag_batch.record or ()
    sags = [item for item in sag_records if isinstance(item, Sag) and item.id == case_id]
    if not sags:
        diagnostics.append(_diag("case_not_found", "Sag", "requested case was not returned", entity_id=case_id))
        return HydrationResult(None, tuple(diagnostics), False)

    raw_stages, stages_complete, found = _safe_scan(client, "Sagstrin", f"sagid eq {case_id}", case_id)
    diagnostics.extend(found)
    stages_batch = _as_batch(raw_stages, "Sagstrin")
    diagnostics.extend(stages_batch.diagnostics)
    stages = [item for item in (stages_batch.record or ()) if isinstance(item, Sagstrin)]
    stages_complete = stages_complete and stages_batch.complete

    raw_links, links_complete, found = _safe_scan(client, "SagDokument", f"sagid eq {case_id}", case_id)
    diagnostics.extend(found)
    links_batch = _as_batch(raw_links, "SagDokument")
    diagnostics.extend(links_batch.diagnostics)
    links = [item for item in (links_batch.record or ()) if isinstance(item, SagDokument)]
    links_complete = links_complete and links_batch.complete

    document_ids = sorted({item.dokumentid for item in links})
    documents: list[Dokument] = []
    documents_complete = True
    for batch_ids in _chunks(document_ids):
        raw_docs, batch_complete, found = _safe_scan(client, "Dokument", _id_filter("id", batch_ids), case_id)
        diagnostics.extend(found)
        documents_complete = documents_complete and batch_complete
        docs_batch = _as_batch(raw_docs, "Dokument")
        diagnostics.extend(docs_batch.diagnostics)
        documents.extend(item for item in (docs_batch.record or ()) if isinstance(item, Dokument))
        documents_complete = documents_complete and docs_batch.complete

    stage_ids = sorted({item.id for item in stages})
    votes: list[Afstemning] = []
    votes_complete = True
    for batch_ids in _chunks(stage_ids):
        raw_votes, batch_complete, found = _safe_scan(client, "Afstemning", _id_filter("sagstrinid", batch_ids), case_id)
        diagnostics.extend(found)
        votes_complete = votes_complete and batch_complete
        vote_batch = _as_batch(raw_votes, "Afstemning")
        diagnostics.extend(vote_batch.diagnostics)
        votes.extend(item for item in (vote_batch.record or ()) if isinstance(item, Afstemning))
        votes_complete = votes_complete and vote_batch.complete

    status_rows: list[dict[str, object]] = []
    status_id = sags[0].statusid
    status_complete = True
    if status_id is not None:
        try:
            status_rows = client.fetch_page("Sagsstatus", {"$filter": f"id eq {status_id}", "$top": 2})
        except ODataError:
            status_complete = False
            diagnostics.append(_diag("status_fetch_failed", "Sagsstatus", "status request failed", entity_id=case_id, related_id=status_id))
    statuses_batch = _as_batch(status_rows, "Sagsstatus")
    diagnostics.extend(statuses_batch.diagnostics)
    statuses = [item for item in (statuses_batch.record or ()) if isinstance(item, Sagsstatus)]
    status_complete = status_complete and statuses_batch.complete
    if status_id is not None and not statuses and status_complete:
        diagnostics.append(_diag("missing_status", "Sagsstatus", "case status reference was not found", entity_id=case_id, related_id=status_id))
        status_complete = False

    normalized_diagnostics = tuple(diagnostics)
    assembled = assemble_cases(
        sags,
        stages=stages,
        document_links=links,
        documents=documents,
        votes=votes,
        statuses=statuses,
        diagnostics=normalized_diagnostics,
    )
    selected = next((item for item in assembled.cases if item.external_id == str(case_id)), None)
    scan_complete = sag_batch.complete and stages_complete and links_complete and documents_complete and votes_complete and status_complete
    if selected is not None and not scan_complete:
        selected = replace(selected, complete=False)
    final_diagnostics = assembled.diagnostics
    complete = selected is not None and selected.complete and scan_complete and not final_diagnostics
    return HydrationResult(selected, final_diagnostics, complete)


__all__ = ["AssemblyResult", "BulkHydrationResult", "HydrationResult", "assemble_cases", "load_case", "load_cases"]
