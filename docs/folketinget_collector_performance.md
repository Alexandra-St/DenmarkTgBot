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
