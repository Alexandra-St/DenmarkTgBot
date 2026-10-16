# Folketinget OData transport (Phase 1.1.2B)

Read-only Python transport for the ODA OData 3.0 API. It returns raw entity dictionaries; it does not join, normalize, classify, or persist them.

## Supported requests

The client accepts only `Sag`, `Sagstrin`, `SagDokument`, `Dokument`, `Afstemning`, and `Sagsstatus`, at `https://oda.ft.dk/api/`. Each has `id` and `opdateringsdato` in the live metadata. Requests require a bounded `$top` (default scan page size 100; maximum 1,000) and support `$filter`, `$select`, `$orderby`, and `$top`. Parameters are URL-encoded. The client sets a timeout, JSON `Accept`, and a descriptive User-Agent; it accepts only HTTPS responses from `oda.ft.dk`.

Example bounded OData 3.0 query:

```text
https://oda.ft.dk/api/Sag?$select=id,opdateringsdato&$orderby=opdateringsdato%20desc,id%20desc&$top=1
```

Keyset continuation after a cursor is equivalent to:

```text
($filter=(opdateringsdato%20gt%20datetime%272026-10-14T08%3A46%3A08.6566667%27%20or%20(opdateringsdato%20eq%20datetime%272026-10-14T08%3A46%3A08.6566667%27%20and%20id%20gt%20106))&$orderby=opdateringsdato%20asc,id%20asc&$top=100
```

## Pagination and timestamps

Each scan first captures the greatest `(opdateringsdato, id)` using descending order and `$top=1`. Data pages are ordered ascending and bounded inclusively by both values. Subsequent pages use the exclusive tuple predicate `updated > cursor_time OR (updated = cursor_time AND id > cursor_id)`. The client continues after a short page and only completes on an empty bounded page or after reaching the upper pair. It checks timestamp syntax, numeric IDs, row order, cursor advancement, and the captured bound; it deduplicates repeated entity IDs by retaining the newest version observed during that scan. `ScanResult.metadata` reports collection, fetched page/row/request counts, upper bound, last cursor, and completion. `record_count` counts rows fetched; `records` contains unique entity IDs. Retrieval or page-validation failures raise `ODataPageError` with an explicit incomplete partial result and the underlying error type; they are never converted to empty-success results.

ODA timestamps are returned as source strings, including up to seven fractional digits. Current metadata declares `Edm.DateTime`; sampled values have no timezone. The transport validates offset-free ODA datetime syntax and uses a padded comparison key without rewriting the raw value, appending `Z`, truncating source text, or converting to UTC.

## HTTP failures and limitations

HTTP 429/5xx and network failures receive at most two retries by default, with capped exponential backoff. Permanent HTTP errors, invalid JSON, empty bodies, invalid OData structure, and unsupported redirects fail without being turned into empty pages. Error text does not include response bodies.

ODA does not promise snapshot isolation. A record edited during a scan may move relative to the cursor; the upper pair bounds the captured run but cannot freeze the collection. Live targeted scans also observed different fractional-second precision for the same timestamp across requests; the collector fails closed and preserves partial rows when this breaks the upper bound or cursor. See [normalization and relationship verification](folketinget_normalization.md) for the observed case. There is no documented rate-limit SLA or stable page-cap guarantee. This phase has no persistent checkpoint or overlap/reconciliation logic. Keep future scans modest and restart an incomplete scan from its prior durable checkpoint.

## Live verification (2026-10-16)

Read-only requests returned HTTP 200 and OData JSON `value` arrays for all six collections. For each collection, a bounded descending maximum-pair request succeeded, followed by an ascending, upper-bounded keyset request with `$top=1`; each second request returned one row. This verifies the generic ordering, datetime literal, compound keyset filter, and bound syntax on each set. Latest observed pairs included `Sag (2026-10-15T21:55:42.37, 106028)`, `Sagstrin (2026-10-15T15:06:50.94, 277501)`, `SagDokument (2026-10-15T15:47:10.6966667, 1553303)`, `Dokument (2026-10-15T15:47:10.6966667, 1236879)`, `Afstemning (2026-10-01T14:21:35.79, 10605)`, and `Sagsstatus (2024-11-27T14:14:11.6866667, 68)`. These are observations, not fixed values.

## Sag type and category references

Live metadata confirms `Sag.typeid` and `Sag.kategoriid` are nullable integer foreign keys. Their reference entity sets are `Sagstype` (`id`, `type`) and `Sagskategori` (`id`, `kategori`), both with `opdateringsdato`. Small reference queries returned 13 type rows and 31 category rows. Observed mappings include `Sagstype.id=3` → `Lovforslag`, `Sagstype.id=5` → `Beslutningsforslag`, and `Sagstype.id=7` → `Kommissionsforslag`. Sampled `Sag` records included type 3 with category 13 (`Regeringsforslag`) and type 3 with category 15 (`Privat forslag`). Thus the type reference can distinguish recorded law proposals from other case types; category gives a proposal subtype where populated. Future relevance filtering should resolve current reference rows by label or use verified IDs bound to those rows, account for `kategoriid=null`, and keep the rule explicit. This transport does not hardcode type/category IDs or filter cases.

No timezone, API snapshot-isolation, rate-limit/page-cap, or deletion-reconciliation guarantee was established.
