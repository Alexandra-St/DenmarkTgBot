# Production readiness: ingestion

## Current state

The collector-to-Worker flow passed locally against a temporary Wrangler D1 database. The local run applied migrations `0001`–`0004`, imported historical fixture records for both sources, then accepted an incremental Folketinget run. A first incremental run is rejected until that source has a committed historical baseline.

Production was checked and updated on 2026-10-16. The checked-in `worker/wrangler.jsonc` and Cloudflare account match Worker `denmarktgbot` and D1 `denmarktgbot-db` (`1bbb417b-2b54-4f77-85ec-d3b94be9c0c8`). Migrations `0001_initial_schema.sql` through `0004_historical_baseline_gate.sql` are recorded as applied; Wrangler reports no pending migrations.

The reviewed Worker build is deployed at `https://denmarktgbot.alexandra-strona.workers.dev`, version `f6562445-5f6a-4075-b695-5e3f4b09a3bc`. `GET /health` returned HTTP 200. Unauthenticated `POST /internal/ingest`, `GET /internal/source-states`, and `POST /telegram/webhook` each returned HTTP 401. Invalid ingestion authorization returned 401. Authenticated read-only source-state requests returned HTTP 200 for both sources, with no state rows/checkpoints initialized.

The Worker has all required secret names: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and `INGEST_API_TOKEN`. Existing Telegram secret values were not read or changed. The new ingestion token is stored in a permission-restricted local file outside the repository; its value is not recorded here.

Post-deployment read-only D1 checks confirmed the original user/source counts and aggregated preferences were unchanged, publications and notifications remain empty, and no source baseline is initialized. The initial authenticated Python request used the default User-Agent and received Cloudflare 1010. Adding the explicit application User-Agent `DenmarkTgBot-IngestionClient/1.0` allowed the same authenticated read-only request to return HTTP 200; authentication and WAF settings were unchanged.

## First production historical collection — 2026-10-16

- **Høringsportalen:** both official ATOM feeds completed. 50 entries parsed, with 25 cross-feed duplicates merged into 25 unique publications. All IDs, official HTTPS URLs, and updated timestamps passed validation; no entries were skipped. Historical dry-run with ruleset 1.1 was complete and `would_submit=true`: 25 records in 1 batch (DIRECT 3, POTENTIAL 10, IRRELEVANT 12), all notification-blocked as historical. This is only the feeds' bounded recent selection, not a complete archive. The two proposed feed checkpoints are independent and both end at ID 71554 / `2026-09-29T06:47:21.000000Z`.
- **Folketinget:** the configured 30-day historical scan did not complete. The ODA TLS handshake stalled during collection; the attempt was interrupted before any result was written. Its output is incomplete and was not dry-run or imported. No partial data was used to initialize a baseline.
- **Production import:** after explicit approval, Høringsportalen run `09c8d363-1f8c-498b-b170-5db9c798f66f` completed in historical mode with `COMMITTED`, 25 accepted, and checkpoints advanced. Read-only D1 verification confirmed all 25 staged rows processed, 25 unique publications stored, all marked historical and complete, and the classification counts matched the dry-run (DIRECT 3, POTENTIAL 10, IRRELEVANT 12). The database has 25 `FIRST_SEEN` events with `notify_eligible=0` and zero notifications. One baseline was initialized, with separate checkpoints for both feeds at ID 71554 / `2026-09-29T06:47:21.000000Z`. The final API response's `processed_records: 5` represented the last commit step; the persisted run and staging rows verify the total of 25.
- **Production safety:** users remain at 1 with the same aggregated preferences (`ru`, direct enabled, potential disabled); migrations remain at 4. Folketinget has no baseline or checkpoint. No Telegram messages were sent, secrets were not read or changed, and no Worker deployment or migration was performed.
- **Backup:** a fresh remote D1 export was restored locally and passed SQLite `integrity_check`; the restored schema contains the expected user/source/publication and ingestion tables. The backup and source snapshots remain outside the repository with restricted permissions.

## Folketinget TLS recovery — 2026-10-16

- A previous interrupted full scan showed Python blocked in `ssl.py` at `SSLSocket.do_handshake`. Bounded probes now resolve `oda.ft.dk` to IPv4, use no configured proxy, and verify the HTTPS certificate normally. Python `urllib` and system curl each returned HTTP 200; curl TLS setup took about 0.14 seconds (`ssl_verify_result=0`). The production ODA client also returned one `Sag` row with `id` and `opdateringsdato` using a 5-second request timeout and 8-second process deadline.
- No DNS, certificate, or single-request timeout failure reproduced. The new 30-day historical collection (72-hour overlap, page size 100, 25-page per-stream cap) did not serialize a result before its 600-second process deadline. It is incomplete; no classification, dry-run, or import was performed. The precise cause of the long-run stall remains unresolved. No code defect was reproduced, so no transport workaround was added.
- Before any potential write, read-only checks confirmed no pending migrations, the authenticated ingestion API returned Folketinget state successfully, and its baseline/checkpoints were absent. Since collection did not pass completeness checks, no Folketinget write was sent. Production remains at 25 Høringsportalen publications, 0 Folketinget publications, 1 Høringsportalen baseline, 0 notifications, and unchanged user preferences.

## Folketinget collector performance — 2026-10-16

- The earlier 600-second historical run's precise stopping point remains unknown. Existing bounded probes did not reproduce DNS, TLS, certificate, or single-request failures.
- Code review identified sequential work as the likely throughput bottleneck: six streams ran serially, followed by repeated per-case relationship requests. The collector now scans streams concurrently with a bounded pool, uses minimal change-stream projections, resolves relationships concurrently, and bulk-hydrates affected cases.
- Per-request and whole-client deadlines are bounded. Progress logs contain counters only. Incomplete scans/hydration still block completion and expose no committable checkpoints. No classification semantics, D1 state, or notification behavior changed.
- No production collection, write, or deployment was run. See `docs/folketinget_collector_performance.md` for the safe first-collection procedure. A real historical collection must still pass all completeness checks and a local dry-run before any separately approved production import.
- Probe metadata, timeout status, and checksums are stored in the protected local source-snapshot directory, outside Git. Python tests passed 119/119; Worker typecheck passed and Worker tests passed 23/23.

## Before first import

1. Run the local checks from the repository root: `python3 -m unittest discover -s tests -v`, then from `worker/`: `npm run typecheck` and `npm test`. All checks passed before deployment (119 Python tests, 23 Worker tests, typecheck passed).
2. Keep the first collection manually initiated. Collect both sources, inspect completeness and diagnostics, and run uploader `--dry-run` before any write.
3. Run historical mode first for each source independently. Do not run incremental mode until that source's historical run reports `COMMITTED` and `checkpoints_advanced: true`; D1 enforces this order. Historical records/events remain ineligible for notifications.
4. The Høringsportalen GitHub Actions cron is configured for 06:17, 14:17 and 22:17 UTC, protected by `HOERINGSPORTALEN_AUTOMATION_ENABLED`; verify the current repository variable before relying on whether scheduled writes are active. Notification queue and delivery remain separate and disabled by default in checked-in Wrangler configuration.

## Historical-first import

Use historical mode first for **each source independently**. Do not run incremental mode until the historical run reports `COMMITTED` and `checkpoints_advanced: true`. The Worker enforces this order in D1. Historical records/events remain ineligible for notifications.

Folketinget historical collection defaults to a 30-day window plus a 72-hour overlap. Høringsportalen feeds expose only a bounded current set (up to 25 entries per feed), so that source cannot provide a full archive baseline; the initial run establishes a starting point for future tracking only. Review `collection_complete` and feed diagnostics before upload. Incomplete collection output is refused before staging.

The uploader accepts at most 200 batches of up to 50 records each (10,000 records per run), also subject to the request-size bound. It does not truncate: the Worker rejects a run declaring more than 200 batches before accepting records. If a full historical collection exceeds this limit, stop. Do not split it into multiple normal historical runs because the first committed run opens incremental mode. A future safe solution must add an explicit multi-run baseline session whose gate is promoted only after every bounded shard has been durably committed and validated.

In a protected terminal, collect each source into a local file, inspect the results, then use uploader dry-run before upload. Collector requests are read-only. The sample uses `/secure/input` as an operator-controlled directory; keep its files out of Git.

```sh
set -e
python3 -m collectors.folketinget_sync historical > /secure/input/folketinget.json
python3 -m collectors.main > /secure/input/hoeringsportalen.json
python3 -m collectors.ingestion_client /secure/input/folketinget.json --source folketinget --mode historical --dry-run
python3 -m collectors.ingestion_client /secure/input/hoeringsportalen.json --source hoeringsportalen --mode historical --dry-run
read -s "INGEST_API_TOKEN?Worker ingestion token: "; export INGEST_API_TOKEN; echo
export INGEST_API_URL="https://denmarktgbot.alexandra-strona.workers.dev"
python3 -m collectors.ingestion_client /secure/input/folketinget.json --source folketinget --mode historical
python3 -m collectors.ingestion_client /secure/input/hoeringsportalen.json --source hoeringsportalen --mode historical
unset INGEST_API_TOKEN INGEST_API_URL
```

Do not continue to incremental collection unless both historical imports are complete and committed. Preserve the same `--run-id` when resuming a partially submitted run. Never abort a run after it has entered `COMMITTING`; retry its commit until it is terminal.

## Operational checks and limits

- Before each run, save collector output and review `complete`, stream/feed status, diagnostics, and proposed record/batch counts. Use uploader `--dry-run` first; it performs no writes.
- After upload, require `status=COMMITTED`, expected processed/accepted counts, and `checkpoints_advanced=true`. Inspect source states and baseline rows through authenticated read-only queries before scheduling any future automation.
- The scheduled Høringsportalen workflow is configured and remains controlled by its repository automation gate. Notification delivery is separate and is not invoked by ingestion.
- `cleanup` defaults to dry-run. Only old `COMMITTED`/`ABORTED` staging and batch rows are eligible; it retains run audit rows and never removes `BLOCKED`/active runs, publications, events, notifications, or checkpoints. Require a reviewed dry-run before any explicit deletion.
- A source or staging failure leaves checkpoints unchanged. Diagnose and resume the same run; do not reset D1 or manually advance cursors.

## References

- [Wrangler D1 commands](https://developers.cloudflare.com/d1/wrangler-commands/) — migration inventory/application and database export commands.
- [D1 import/export and backups](https://developers.cloudflare.com/d1/best-practices/import-export-data/) — backup and export guidance.
- [Wrangler Worker commands](https://developers.cloudflare.com/workers/wrangler/commands/workers/) — Worker deployment and secret management behavior.
