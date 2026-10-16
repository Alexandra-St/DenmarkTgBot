# Folketinget normalization and relationships (Phase 1.1.2C+D)

`collectors/folketinget_models.py` normalizes ODA dictionaries into frozen dataclasses for `Sag`, `Sagstrin`, `SagDokument`, `Dokument`, `Afstemning`, and `Sagsstatus`. `normalize_many()` keeps valid partial records and returns structured diagnostics for invalid IDs, foreign keys, fields, dates, and timestamps. Required IDs and non-nullable relationship IDs must be integers; nullable ODA foreign keys remain `None`. Danish text and source date strings are kept as published.

## Case fields and joins

`assemble_cases()` in `collectors/folketinget_relations.py` returns `LegislativeCase` objects with string `external_id`, source `folketinget`, number/title, `type_id`, `category_id`, case status `{id, name}`, raw update time, nullable UTC time, the verified ODA entity URL, optional `legal_url`, stages, documents, votes, and a `complete` flag. `to_dict()` produces JSON-compatible dictionaries. Type/category identifiers are preserved as data only; there is no relevance rule.

Relationships use numeric IDs only:

- `Sag.id → Sagstrin.sagid` attaches stages; stage status IDs stay on stages and are not decoded through the case status table.
- `Sag.id → SagDokument.sagid → SagDokument.dokumentid → Dokument.id` attaches documents. Each child retains both the document ID and link ID, so shared documents and distinct links stay distinct.
- `Sagstrin.id → Afstemning.sagstrinid → Sagstrin.sagid` attaches votes. A null or missing stage link stays unresolved and is never guessed.
- `Sag.statusid → Sagsstatus.id` resolves Danish case status. A missing status label preserves its ID and marks the case incomplete.

Children are sorted by official IDs. Identical duplicate records collapse. Conflicting records with the same entity ID produce `duplicate_conflict` and use the lexicographically smallest canonical JSON representation for deterministic output. Missing document targets retain the link as a partial document item; a failed document scan is reported separately and is not mislabeled as a confirmed missing target. Orphan stages, links, documents, and votes remain in structured diagnostics.

## Timestamps and source dates

Every update time keeps its exact `opdateringsdato` string in `source_updated_at_raw`. Offset-free values, including seven-digit fractions, keep `updated_at_utc=null`; no timezone is inferred. Offset-aware values are converted to UTC for `updated_at_utc`, while the raw value remains available. Derived UTC text uses Python's microsecond precision, so the original source string is the precision-preserving field. `dato`, `modtagelsesdato`, and `frigivelsesdato` remain separate raw source dates; they are not used as update or publication times. A case creation date and a vote event date are not synthesized.

## Targeted hydration

`load_case(client, case_id)` fetches one case by numeric ID, then uses the existing bounded `ODataClient.scan()` with restrictive `sagid`, document-ID, or stage-ID filters. Related IDs are queried in groups of at most 50; status is fetched by its exact ID. There is no whole-table scan, persistent cache, or network work in the assembler. Failed scans return usable partial records with `related_page_failed` or `related_hydration_failed` diagnostics and set the case incomplete; failed status requests preserve the status ID and mark the case incomplete. No checkpoint is written.

Example shape:

```json
{
  "external_id": "106028",
  "source": "folketinget",
  "number": "L 34",
  "title": "Forslag til lov om ændring af lov om leje.",
  "type_id": 3,
  "category_id": 13,
  "status": {"id": 25, "name": "Fremsat"},
  "source_updated_at_raw": "2026-10-15T21:55:42.37",
  "updated_at_utc": null,
  "source_url": "https://oda.ft.dk/api/Sag(106028)",
  "legal_url": null,
  "stages": [],
  "documents": [{"id": 1234352, "link_id": 1551000}],
  "votes": [],
  "complete": true
}
```

## Controlled live verification (2026-10-16)

`Sag(106028)` hydrated with status `Fremsat` (ID 25), four document links resolved to documents, no stages or votes, and `complete=true`. Its official entity URL and offset-free timestamp were retained; no legal URL was present.

A second targeted sample, `Sag(71032)` (`L 23`), confirmed status `Stadfæstet`, stage links and vote `Afstemning(10605) → Sagstrin(274943) → Sag(71032)`. ODA's result for `Sagstrin(274944)` differed between the high-water and bounded page responses (`2026-10-01T11:32:44.3666667` versus `2026-10-01T11:32:44.367`); a related `SagDokument` continuation also returned an already-seen ID with a different fractional precision. The collector therefore marked this hydration incomplete and retained the valid partial stages, links, and vote instead of reporting a complete case. This is an observed ODA consistency/precision limitation, not a guessed relationship.

ODA does not promise snapshot isolation or stable serialization precision. An incomplete targeted scan must remain incomplete; later persistence and retry/reconciliation policy are outside this phase.
