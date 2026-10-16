# Folketinget OData collector: design (Phase 1.1.2A)

**Scope:** a one-run Python collector producing JSON. No storage, classification, notifications, Worker changes, or deployment. `Verified` means checked against the existing [source research](source_research.md), [official documentation](https://www.ft.dk/da/dokumenter/aabne_data), or a read-only ODA response on 2026-10-16. `Proposed` means an implementation decision that still needs tests.

## 1. Verified API and relationships

The service root is <https://oda.ft.dk/api/>; [metadata](https://oda.ft.dk/api/$metadata) identifies OData 3.0 entities and fields. These collections were verified in prior research; this design rechecked their schemas in live metadata (HTTP 200):

| Collection | Fields needed here | Join |
| --- | --- | --- |
| [`Sag`](https://oda.ft.dk/api/Sag) | `id`, `nummer`, `titel`, `statusid`, `opdateringsdato`, `retsinformationsurl` | `id` is the case key; `statusid → Sagsstatus.id`. |
| [`Sagstrin`](https://oda.ft.dk/api/Sagstrin) | `id`, `sagid`, `titel`, `dato`, `statusid`, `opdateringsdato` | `sagid → Sag.id`. |
| [`SagDokument`](https://oda.ft.dk/api/SagDokument) | `id`, `sagid`, `dokumentid`, `bilagsnummer`, `frigivelsesdato`, `opdateringsdato` | `sagid → Sag.id`, `dokumentid → Dokument.id`. The link has its own ID and update time. |
| [`Dokument`](https://oda.ft.dk/api/Dokument) | `id`, `titel`, `dato`, `opdateringsdato` | Reach cases through `SagDokument`; no direct `sagid`. |
| [`Afstemning`](https://oda.ft.dk/api/Afstemning) | `id`, `sagstrinid`, `nummer`, `vedtaget`, `konklusion`, `opdateringsdato` | `sagstrinid → Sagstrin.id → Sag.id` when populated. A real record with `sagstrinid = null` was seen in prior research. |
| [`Sagsstatus`](https://oda.ft.dk/api/Sagsstatus) | `id`, `status`, `opdateringsdato` | Resolves `Sag.statusid` to Danish status text. |

Join by these IDs, never by title or case number. A missing target or null foreign key remains unresolved in the output/diagnostics; do not infer a link from a meeting or similar title. `Sagstrin.statusid` belongs to a separate stage-status catalogue and must not be decoded with `Sagsstatus`.

**Verified requests:** `$filter`, `$select`, `$top`, `$skip`, `$orderby` and `$expand` worked in prior research. A new bounded `Sag` request returned HTTP 200 with `$orderby=opdateringsdato asc,id asc`; a second request confirmed the keyset predicate `(opdateringsdato > last_time) OR (opdateringsdato = last_time AND id > last_id)`. The official API entity URL `https://oda.ft.dk/api/Sag(106028)` also returned HTTP 200. Use that as the official `source_url` until a human-facing case URL is verified. `Sag.retsinformationsurl` is a separate optional legal-information link, not a substitute case URL.

## 2. JSON case representation (proposed)

One case object keyed by `source="folketinget"` and `external_id=str(Sag.id)`. Required for a usable case: valid `Sag.id`; preserve a record with missing other fields and flag it incomplete. Do not translate Danish text.

| Field | Type and source |
| --- | --- |
| `external_id`, `source` | Required string ID and constant source code. |
| `number`, `title` | Nullable strings from `Sag.nummer`, `Sag.titel`. |
| `status` | `{id: int|null, name: str|null}` from `Sag.statusid` and `Sagsstatus.status`; keep ID if lookup fails. |
| `source_updated_at_raw`, `updated_at_utc` | Original `Sag.opdateringsdato` string; UTC ISO 8601 string **only after its timezone is established**, otherwise `null`. |
| `source_url`, `legal_url` | Verified OData `Sag(id)` URL; optional `Sag.retsinformationsurl`. |
| `stages[]` | `id`, `title`, `date_raw`, `source_updated_at_raw`, nullable `updated_at_utc`, optional stage status ID. |
| `documents[]` | `id`, `link_id` (`SagDokument.id`), `title`, `date_raw`, `released_at_raw`, `source_updated_at_raw`, nullable `updated_at_utc`. |
| `votes[]` | `id`, `stage_id`, `number`, `approved` (`vedtaget`), `conclusion`, `source_updated_at_raw`, nullable `updated_at_utc`. |

Arrays sort by official numeric ID and deduplicate by entity ID (link records by `SagDokument.id`). No case creation date or vote occurrence date is invented: `Afstemning` has no direct event date in this scope. A vote's `vedtaget` is the recorded vote result, not proof that a law entered into force. Preserve raw source fields needed for later comparison. The JSON envelope should include per-stream success/failure and proposed checkpoints, as the Høringsportalen collector does for feeds.

**Timestamp caveat:** metadata declares `opdateringsdato` as `Edm.DateTime`, and a live `Sag` returned `2026-10-14T08:46:08.6566667` without an offset. The official documentation describes update time but does not specify its timezone. Preserve the seven-digit raw value for OData filters, ordering and checkpoints; Python microsecond conversion would lose a digit. Convert offset-bearing values to UTC. For offset-free ODA values, leave `updated_at_utc=null` until the source timezone is verified; do not append `Z` or assume Copenhagen time. This is an explicit prerequisite for a trustworthy UTC field.

## 3. Incremental collection and pagination (proposed)

Track six independent streams: `Sag`, `Sagstrin`, `SagDokument`, `Dokument`, `Afstemning`, `Sagsstatus`. Each has its own last **completed** source update watermark, matching the future `(source_id, stream_key)` convention in [database_schema.md](database_schema.md). The design does not write those checkpoints yet.

1. At run start, read each stream's maximum `(opdateringsdato,id)` with descending order and `$top=1`; use its timestamp as that stream's inclusive upper bound. Begin at its previous completed watermark minus a configurable overlap (initial recommendation: 72 hours). For initial import, use the selected historical floor. Keep source-precision timestamps for OData predicates.
2. Query a bounded window using `$filter`, `$orderby=opdateringsdato asc,id asc`, `$top=100` and a narrow `$select`. Follow pages by `(time,id)` keyset, not offset: the next filter adds `time > last_time OR (time = last_time AND id > last_id)`. Stop at an empty page or the captured upper bound; do not assume that a short page is the last page. Check that each page advances the key. The verified `Sag` predicate must be checked on each other collection in the fetching phase.
3. Deduplicate within and across the overlap by `(entity, id, raw opdateringsdato)`; when the same ID has a newer version, retain the latest. If the same ID/time has conflicting content, report it rather than silently choosing a winner.
4. Build an affected-case set: changed `Sag` → own ID; `Sagstrin`/`SagDokument` → `sagid`; changed `Dokument` → reverse-query `SagDokument` by `dokumentid`; changed `Afstemning` → fetch its `Sagstrin` by `sagstrinid`, then use `sagid`. A null or absent join becomes an unresolved record. A changed `Sagsstatus` refreshes its label; affected cases must be refreshed or resolve status labels from the current catalogue when read.
5. Hydrate only affected cases and their current stage/link/document/vote records. Resolve status IDs even when their catalogue rows predate the scan window. Batch ID filters in small chunks if verified; cache repeated lookups. Mark a case incomplete when a dependency fails. Emit partial JSON with stream errors, but offer a candidate checkpoint only for a stream whose pages and required joins completed. The later consumer advances that checkpoint only after durably accepting its complete output. Failed streams retain their old checkpoints; an interrupted run restarts from the previous completed watermark.

The source does not promise snapshot isolation. Keyset paging avoids `$skip` shifts from inserted rows; the upper bound defers newer updates to the next run. Overlap recovers many late or concurrently moved records, but not arbitrarily old backfills. `$skip` is verified but should be only a fallback with explicit gap risk. Do not fetch the entire database each run. Rate limits and server page caps are undocumented; use timeouts, bounded retries and modest page sizes.

## 4. Baseline and future event detection

For the first 30 days, query **all six streams** for `opdateringsdato` in the preceding 30-day window, then hydrate referenced older cases. This finds records last updated during the window, including cases created earlier. `Sag` metadata has no creation timestamp: the API fields checked here cannot reliably distinguish a case created in that window from an older case updated then. Record `first_seen_at` only as collector observation time, not source publication time. If a strict creation-date import is required, verify an additional authoritative field or procedure before claiming completeness. Until the ODA timezone is confirmed, widen the raw-time boundary conservatively and label it an approximate 30-day baseline.

Baseline output must be marked `historical=true`; the later persistence/event stage must suppress automatic notifications (`notify_eligible=0` in the existing D1 model). After baseline, compare official IDs and meaningful snapshots: unseen `Sag.id` → candidate new case; changed case title/status → modified/status change; new `SagDokument` link or `Dokument.id` → related document; new `Sagstrin.id` → stage; new `Afstemning.id` or changed result → vote. A mere `opdateringsdato` change is not automatically a user-facing event. Event generation is outside this phase.

## 5. Failures and limits

- HTTP 429/5xx and network timeouts: bounded retry/backoff; other HTTP errors: report immediately. Invalid JSON, missing `value`, a nonadvancing page, or an invalid ID fail the affected stream. Never treat a failed page as an empty page.
- Invalid timestamps: retain the raw value, flag the record/stream, and do not advance a checkpoint beyond it until resolved. Missing optional fields become `null`. Missing joins are reported and excluded from case aggregation until resolved; do not attach orphan votes to a guessed case.
- Deleted records and links may not appear as updates. Folketinget documents a separate `slettet` table; deletion reconciliation is a later requirement. `SagstrinDokument` provides stage-specific document links and is outside this minimal case-document path.
- ODA exposes current records rather than a guaranteed version history. Multiple edits between scans can collapse into one observed state; intermediate status transitions cannot be reconstructed reliably.
- The current D1 `publications` row can hold a case ID/title/status/update time, and `source_states` can hold per-stream checkpoints. `source_updated_at` is nullable and should stay null until raw ODA time can be converted to UTC safely. The schema has no dedicated structure for stage, document-link or vote snapshots. Their durable storage and comparison need a later persistence design; no schema change is made here.
- Source data may change or be unavailable, and the API has no stated publication latency or rate-limit SLA. Cite Folketinget as the data source.

## 6. Small implementation phases for Luna

Keep the existing `collectors/hoeringsportalen.py` and its CLI untouched. Proposed files: `collectors/folketinget.py` (OData requests, models, normalization and stream scan), `collectors/folketinget_relations.py` (ID joins and case assembly), `collectors/folketinget_main.py` (one-run JSON CLI), plus `tests/test_folketinget.py` with mocked pages/joins.

1. **Transport and paging:** one collection, bounded errors, snapshot upper bound and keyset pages; test ties, empty/short pages, retries, interruption. Verify the predicate on every entity.
2. **Parsing and dates:** typed raw entities and case fields; test nulls, Danish text, seven-digit precision, invalid values and explicit UTC policy. Resolve the timezone question before emitting UTC timestamps.
3. **Relationships:** join all six entity types; test one-to-many links, changed document reverse lookup, missing links and null `sagstrinid`.
4. **Incremental run:** per-stream overlap, affected-case set, partial failures and proposed checkpoints; test restart without lost pages or false advancement.
5. **CLI:** deterministic JSON plus stderr diagnostics and a 30-day historical flag; test without network and confirm no Worker or D1 calls.

**Open questions:** authoritative timezone for ODA `Edm.DateTime`; authoritative case creation date; continuation/page cap behavior on all collections; how to reconcile `slettet` and stage-only document links; and how later persistence will store relationship snapshots. These are explicit implementation checks, not assumed API guarantees.
