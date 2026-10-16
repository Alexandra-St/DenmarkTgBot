# Production readiness: ingestion

## Current state

The collector-to-Worker flow passed locally against a temporary Wrangler D1 database. The local run applied migrations `0001`–`0004`, imported historical fixture records for both sources, then accepted an incremental Folketinget run. A first incremental run is rejected until that source has a committed historical baseline.

Production has **not** been checked or changed in this phase. The checked-in `worker/wrangler.jsonc` points to Worker `denmarktgbot` and D1 `denmarktgbot-db` (`1bbb417b-2b54-4f77-85ec-d3b94be9c0c8`). It contains no `INGEST_API_TOKEN` binding. Do not assume the production migration state or token configuration: both need operator verification. No Cloudflare deployment, remote migration, source collection, Telegram call, or secret change was performed.

## Before any production run

1. Review this checklist and approve the production change window. Confirm the Cloudflare account is on the intended free plan and the Worker/D1 names and ID above match the dashboard.
2. Run the local checks from the repository root:

   ```sh
   python3 -m unittest discover -s tests -v
   cd worker
   npm run typecheck
   npm test
   ```

3. From `worker/`, inspect the remote migration inventory. This is read-only:

   ```sh
   npx wrangler d1 migrations list denmarktgbot-db --remote
   ```

   Confirm `0001`–`0003` are applied and `0004_historical_baseline_gate.sql` is pending. Stop if the inventory differs; do not recreate the database or rerun applied migrations.

4. Back up the existing D1 database to a restricted location and verify the export exists before continuing. Do not place it in the repository:

   ```sh
   npx wrangler d1 export denmarktgbot-db --remote --output=/secure/backups/denmarktgbot-before-ingestion-baseline.sql
   ```

5. Apply only the pending migration, after the backup and change approval:

   ```sh
   npx wrangler d1 migrations apply denmarktgbot-db --remote
   npx wrangler d1 migrations list denmarktgbot-db --remote
   ```

6. Configure `INGEST_API_TOKEN` as a Worker Secret in the Cloudflare dashboard. Use a newly generated secret, distinct from every Telegram credential. Never paste it into a command, repository file, or chat. Verify the secret **name** in the dashboard without copying its value.
7. Deploy the reviewed Worker build from `worker/`:

   ```sh
   npm run deploy
   curl --fail --show-error https://denmarktgbot.alexandra-strona.workers.dev/health
   ```

   The health response must be HTTP 200. Verify `POST /internal/ingest` and `GET /internal/source-states` return HTTP 401 without authorization. Then verify an authorized `GET /internal/source-states?source=folketinget` and `...?source=hoeringsportalen` using the operator's protected local environment; never print the token.

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
- Keep the first production run manually initiated. There is no scheduler or notification delivery in this phase.
- `cleanup` defaults to dry-run. Only old `COMMITTED`/`ABORTED` staging and batch rows are eligible; it retains run audit rows and never removes `BLOCKED`/active runs, publications, events, notifications, or checkpoints. Require a reviewed dry-run before any explicit deletion.
- A source or staging failure leaves checkpoints unchanged. Diagnose and resume the same run; do not reset D1 or manually advance cursors.

## References

- [Wrangler D1 commands](https://developers.cloudflare.com/d1/wrangler-commands/) — migration inventory/application and database export commands.
- [D1 import/export and backups](https://developers.cloudflare.com/d1/best-practices/import-export-data/) — backup and export guidance.
- [Wrangler Worker commands](https://developers.cloudflare.com/workers/wrangler/commands/workers/) — Worker deployment and secret management behavior.
