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

Bulk lookups now preserve HTTP status codes and split a group into at most 16 IDs only after HTTP 400 and only when the original group exceeds that proven working size. Other failures are not retried. A 400 at 16 IDs or fewer remains a blocking error. Logs report entity, group size, URL character count, status category, and whether a split occurred; they do not include URLs or IDs.

The same historical snapshot also reported `ODataPaginationError` for streams `Dokument`, `Sag`, `SagDokument`, and `Sagstrin`. Its log stored only the exception class, not the specific cursor/order condition. That separate stream-pagination cause remains uncertain and will still prevent a complete collection. The next safe validation is a bounded read-only bulk hydration of the previously affected case IDs, followed by a separate small pagination diagnosis; do not rerun the full 30-day collection until the stream-pagination failure is understood.
