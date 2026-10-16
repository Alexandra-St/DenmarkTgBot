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
