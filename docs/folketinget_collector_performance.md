# Folketinget collector performance

## Findings

The earlier 30-day ODA run exceeded its 600-second process deadline. Bounded DNS, TLS, and one-row HTTPS requests succeeded, so those probes did not reproduce a transport failure. The exact cause of that historical timeout is unknown because no per-request trace from that run was retained.

Code inspection found two costs that grow with collection size: the six ODA streams were scanned one after another, and relationship hydration fetched each affected case and its related rows through repeated requests. This sequential request pattern is a strong, reproducible code-level bottleneck, though it cannot by itself prove which request stalled in the earlier run.

## Changes

- Scan the six independent streams with a bounded worker pool (default four; maximum six).
- Select only IDs, update timestamps, and foreign keys while scanning change streams. Full substantive fields are still loaded during case hydration.
- Resolve reverse relationships concurrently and hydrate affected cases in bounded batches, reducing per-case request fan-out.
- Bound each request (default 15 seconds) and the complete ODA client's network lifetime (default 480 seconds). Retries remain bounded and respect the remaining deadline.
- Emit progress counters for streams, pages, requests, relationship groups, and hydrated cases. Logs contain no publication titles, IDs, or response bodies.
- Any timeout, failed page, failed relationship group, or incomplete hydration keeps the collection incomplete. Partial records and diagnostics remain available; no committable checkpoints are emitted.

The six stream checkpoints remain independent. This collector does not persist or advance checkpoints. Classification and record normalization are unchanged.

## Safe first real collection plan

1. Confirm the Folketinget production baseline is still absent and obtain a fresh verified D1 backup before any later write phase.
2. Run only the read-only collector to a JSON file outside the repository, with bounded settings, for example:

   ```sh
   python3 -m collectors.folketinget_sync historical \
     --days 30 --overlap-hours 72 --page-size 100 --max-pages 1000 \
     --request-timeout 15 --total-timeout 480 --max-workers 4 \
     > /secure/local/path/folketinget-history.json
   ```

3. Require exit status 0, `complete: true`, all six streams complete, no blocking diagnostics, and complete relationship hydration. A timeout or configured page limit is a failed collection, never a partial baseline.
4. Run the existing local classification and ingestion dry-run. Review unique cases, DIRECT/POTENTIAL/IRRELEVANT counts, incomplete records, and batch count.
5. Stop before production ingestion for explicit review and approval. Never manually write checkpoints; only the normal durable commit path may initialize them.

No production collection, D1 write, or deployment was performed for this change.

## Bulk-filter failure diagnosis — 2026-10-16

The first real collection log and JSON snapshot showed 242 affected cases. Relationship hydration made seven groups for each of `Sagstrin` and `SagDokument`: six groups of 40 IDs failed on their initial upper-bound request (page 0, one request), while the final two-ID group succeeded. The failed IDs produced HTTP-error diagnostics; the log did not retain the HTTP status or request length. Reconstructing the exact query shape from the saved diagnostics gives URL lengths of 843–885 characters for `Sagstrin` and 846–888 for `SagDokument`.

Bounded HTTPS probes to official `oda.ft.dk` reproduced HTTP 400 for 40-ID grouped OR filters on both entities. Equivalent filters with 8, 12, and 16 IDs returned HTTP 200. Probe URL lengths were 885/888 characters for 40 IDs and 429/432 for 16 IDs. The evidence identifies an ODA rejection of these oversized OR groups; it does not distinguish a filter-complexity limit from a URL-length limit. No response body or full query URL was retained.

The first 40-ID fallback to 16 was not sufficient for paged scans: on a real 16-ID group, the upper-bound request and first page succeeded, but the next keyset page returned HTTP 400 at 768 URL characters. In the targeted 8-case validation, upper-bound probes succeeded for 1, 4, 8, and 16 actual IDs; full scans for 8 IDs succeeded for `Sag`, `Sagstrin`, and `SagDokument`. The conservative adaptive target is now at most 8 IDs after HTTP 400. Other failures are not retried. A 400 at 8 IDs or fewer remains a blocking error. Logs report entity, group size, failing page, URL character count, status category, and whether a split occurred; they do not include URLs or IDs.

## Pagination validation — 2026-10-16

A bounded read-only hydration of eight saved case IDs returned all eight cases and retained 31 stages and 36 document links, but none could be marked complete because a document subquery failed. A 40-case run using the new 8-ID fallback returned 37 cases, of which 8 were complete; remaining relationship groups failed closed. Repeating a document group showed the page-1 response was sorted, but page 2 returned the exact same `(opdateringsdato, id)` cursor row even though the generated filter uses a strict `gt` tie-break. The failure was reproducible and classified as `cursor_not_advanced`; a direct query for that row with `opdateringsdato gt <same timestamp>` also returned it, while `eq` returned no rows. The tested cursor had seven fractional digits. This points to inconsistent ODA datetime/filter evaluation, but the precise server-side behavior is not established.

The collector now records safe pagination categories (`cursor_not_advanced`, `above_captured_upper_bound`, `page_limit`, and HTTP status), failing page number, and request URL character count. It still fails closed on repeated or out-of-order cursors and does not discard rows or advance checkpoints. Timestamp comparison, seven-digit source precision, ID tie-breaking, and upper-bound checks have not been weakened.

The same historical snapshot also reported `ODataPaginationError` for streams `Dokument`, `Sag`, `SagDokument`, and `Sagstrin`. Its log stored only the exception class, not the specific cursor/order condition, so the original stream failures remain uncertain. The targeted hydration reproduced a cursor repeat on one related-document group. Do not rerun the full 30-day collection until targeted page-by-page validation confirms whether that behavior is transient or systemic and all selected groups complete.

## ODA cursor-filter investigation — 2026-10-16

The original failing page's exact cursor was not retained in the JSON snapshot or log, so it could not be replayed byte-for-byte. A bounded probe used one saved document ID to fetch that document's current ODA row, then tested its returned `opdateringsdato` without recording the ID, timestamp, response body, or URL.

For that current row, an ID-only lookup returned one row. Exact timestamp `gt`, `eq`, `ge`, and `lt` requests all returned HTTP 200; `eq` returned no rows and `gt` did not return the cursor row. The exact timestamp compound keyset filter also did not return that row. A three-digit fractional timestamp variant returned the row for `gt`, which is consistent with the source timestamp being later than the truncated threshold and is not evidence that strict `gt` returned an equal cursor. Timestamp filters combined with the same ID equality returned zero rows for `gt`, `eq`, and `ge`, despite the ID-only result. That is an additional inconsistent observation, not an established general ODA rule. Each request used a maximum of ten rows and exact URL-decoding round trips were confirmed. Small ascending and descending probes both matched their requested `(opdateringsdato, id)` ordering.

The official `$metadata` response identifies `opdateringsdato` as `Edm.DateTime` (50 properties across entity types); it declares no `Precision` attribute. Seven fractional digits are present in actual source rows, but the metadata does not document that precision. The collector emits OData v3-style `datetime'…'` literals. The probe did not establish whether ODA rounds, truncates, or otherwise interprets fractional digits when evaluating comparisons.

Requests included `Cache-Control: no-cache, no-store` and `Pragma: no-cache`; responses had no `Age` or `CF-Cache-Status` header. This does not prove that caching is absent. The prior captured run still provides evidence that one real page response repeated the same cursor despite the strict filter, but this bounded replay did not reproduce that exact result. OData datetime parsing/precision, filter evaluation, server-side pagination/sorting, transient data behavior, and intermediary caching remain hypotheses; none is confirmed as the root cause.

No safe alternate pagination strategy is established. Lowering precision can include rows later than a truncated threshold and cannot be used as a strict continuation cursor. Dropping the timestamp condition or discarding repeated rows could skip records or falsely mark a scan complete. Keep the collector fail-closed on non-advancing cursors and preserve the incomplete result. A future targeted probe should capture the exact failing cursor in memory and issue the comparison requests immediately against the same official endpoint, without exposing its values in logs. Full historical collection remains blocked until representative scans complete without cursor or relationship-hydration failures.

## Reliable pagination investigation — 2026-10-16

Folketinget documents ODA as OData 3.0. Its metadata identifies `opdateringsdato` as `Edm.DateTime`; the metadata does not declare a precision. The OData 3.0 literal grammar permits a `datetime'…'` value with zero through seven fractional digits. The standard defines `gt`, `ge`, `eq`, and `lt` as strict greater-than, greater-than-or-equal, equality, and less-than comparisons. The confirmed ODA behavior—an equal cursor returned for `gt` at every tested precision while absent for `eq`—therefore contradicts the expected filter semantics. The observable failure is confirmed; its internal provider/database cause is unknown. An alternate literal for another type such as `Edm.DateTimeOffset` is not a valid workaround for this `Edm.DateTime` property.

Three bounded live `Dokument` requests compared ordered pages: the first two rows, the next two with `$skip=2`, and the next two with `id gt <last first-page ID>`. The offset and ID-based pages matched exactly, had no overlap, and IDs advanced. This demonstrates that both forms work for that small response only. The official browser manual describes IDs as unique identifiers, but neither a stable snapshot nor ID monotonicity under concurrent writes is established. Offset positions can shift when records are inserted or removed. ID keysets avoid timestamp comparisons but do not by themselves provide snapshot isolation or detect concurrent deletions/updates. Neither method is yet proven complete for a full live scan.

The bounded response did not advertise an OData continuation link. OData 3.0 defines `$skip` and includes `$skiptoken` syntax, but a skip token is service-generated opaque state; guessing one is not valid. No official ODA documentation or response evidence for an ODA `$skiptoken` was found. Time-window partitioning still depends on the same unreliable timestamp predicates and is not safe. The official ODA browser manual documents a nightly full-database backup and updating it by querying every entity after the backup timestamp. The full backup is a plausible historical seed, but it was not downloaded or restored here; its incremental catch-up path relies on the timestamp filter implicated by these findings. It is not yet an end-to-end verified import source.

Conclusion: no demonstrated API pagination strategy currently guarantees complete, duplicate-safe retrieval. Keep ODA scans and checkpoint advancement fail-closed. Do not resume the API-based full historical collection. Reconsider a baseline only after validating the official backup artifact and a safe, complete way to reconcile changes after its snapshot point.

References: [Folketinget Open Data and OData 3.0 documentation](https://www.ft.dk/dokumenter/aabne_data), [ODA browser manual](https://www.ft.dk/-/media/sites/ft/pdf/dokumenter/aabne-data/oda-browser_brugervejledning.pdf), [OData 3.0 URL conventions](https://www.odata.org/documentation/odata-version-3-0/url-conventions/), and [OData 3.0 Atom/type lexical forms](https://www.odata.org/documentation/odata-version-3-0/atom-format/).
