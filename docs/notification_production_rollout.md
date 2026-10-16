# Notification infrastructure: staged production rollout

**State:** preparation only. No production migration, Worker deployment, notification flag change, or message delivery was performed for this phase. The checked-in Wrangler configuration does not set notification variables. Cloudflare dashboard values are runtime configuration and must be verified separately before rollout.

## Safety properties verified locally

- `/internal/notifications/preview`, `/prepare`, and `/deliver` use the existing ingestion bearer-token authorization. No unauthenticated route can preview or invoke queueing/delivery.
- Preview performs aggregate-only SQL reads. It returns category/event/recipient counts and historical/stale suppression counts; it does not return account IDs or alter notification rows/statuses.
- Queue preparation requires `NOTIFICATION_QUEUE_ENABLED=true`, a valid reached `NOTIFICATION_ACTIVATION_CUTOFF`, and a run recorded as `COMMITTED` with `mode='incremental'`. Only that run's event keys are considered. Existing history, Høringsportalen `FIRST_SEEN`, stale events, incomplete records, ineligible events, and unselected categories are suppressed.
- Delivery separately requires `NOTIFICATION_DELIVERY_ENABLED=true` and a valid reached `NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF`. Defaults are absent/disabled. The workflow and Telegram webhook do not invoke queueing or delivery.
- Migration 0005 was applied to a temporary copy restored from the local production-compatible export `pre-github-actions-20261016-170039.sql` (restored SQLite copy). The source database had 1 user, 25 publications, 25 events, 0 notifications, 2 runs, 2 checkpoints, 1 baseline, 50 staging rows and 2 batches. All rows in users, publications, events, runs, checkpoints, baseline, staging, batches and notifications matched before/after migration. `integrity_check=ok`; `foreign_key_check` returned zero violations. A second disposable copy with synthetic PENDING/SENT/FAILED rows preserved all three notification rows. This validates the migration against a production-compatible snapshot, not today's live database; create a fresh backup immediately before any production migration.

## Rollout prerequisites

Use the repository's normal macOS Terminal. Before changing anything, confirm the repository and Cloudflare identity/database:

```sh
cd /Users/alexandra/Projects/DenmarkTgBot/worker
npx wrangler whoami
npx wrangler d1 info denmarktgbot-db
```

Confirm the account is the expected Cloudflare account and the D1 ID is `1bbb417b-2b54-4f77-85ec-d3b94be9c0c8`; inspect `.github/workflows/hoeringsportalen-incremental.yml` and `worker/wrangler.jsonc` for the expected workflow, Worker and binding. Stop if anything differs. This plan intentionally stops at every unexpected result. Never restore a full backup automatically: production writes after the backup could be lost.

### A. Pause scheduled ingestion

The cron trigger is already present (`17 6,14,22 * * *` UTC). Before changing anything, record whether the current repository variable is unset, exact `true`, or another value in the repository settings. Temporarily set it to a non-true value, for example:

```sh
gh variable set HOERINGSPORTALEN_AUTOMATION_ENABLED --body false
```

Or use **GitHub → Settings → Secrets and variables → Actions → Variables**. Do not alter notification settings. Verify the variable is no longer exact lowercase `true` in GitHub settings. At step H restore its exact prior state; if it was unset, delete the temporary variable rather than leaving `false`.

### B. Confirm the workflow is idle

```sh
gh run list --workflow hoeringsportalen-incremental.yml --status in_progress --limit 20
```

Wait until no workflow run is in progress. Do not migrate while an ingestion run is active. If a run appears stuck during production submission/commit, stop and investigate; do not proceed by blindly cancelling it. After it finishes, inspect its Actions summary and require both feeds complete and ingestion `COMMITTED` or a clearly failed/aborted run.

### C. Create and verify a fresh backup

```sh
set -eu
umask 077
BACKUP_DIR="$HOME/DenmarkTgBot-backups"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
SQL_FILE="$BACKUP_DIR/denmarktgbot-${STAMP}.sql"
RESTORE_FILE="$BACKUP_DIR/denmarktgbot-${STAMP}-verify.sqlite"
cd /Users/alexandra/Projects/DenmarkTgBot/worker
npx wrangler d1 export denmarktgbot-db --remote --output="$SQL_FILE"
test -s "$SQL_FILE"
sqlite3 "$RESTORE_FILE" < "$SQL_FILE"
chmod 600 "$SQL_FILE" "$RESTORE_FILE"
test -s "$RESTORE_FILE"
test "$(stat -f %Lp "$SQL_FILE")" = 600
test "$(stat -f %Lp "$RESTORE_FILE")" = 600
sqlite3 "$RESTORE_FILE" 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
shasum -a 256 "$SQL_FILE"
```

Require `integrity_check` to return `ok`, no rows from `foreign_key_check`, a non-empty restricted SQL export, and expected tables (`users`, `publications`, `publication_events`, `notifications`, `ingestion_runs`, `source_states`, `source_ingestion_baselines`). Record aggregate counts and preference totals before migration using read-only SQL:

```sh
npx wrangler d1 execute denmarktgbot-db --remote --command "SELECT 'users', COUNT(*) FROM users UNION ALL SELECT 'publications', COUNT(*) FROM publications UNION ALL SELECT 'events', COUNT(*) FROM publication_events UNION ALL SELECT 'notifications', COUNT(*) FROM notifications UNION ALL SELECT 'runs', COUNT(*) FROM ingestion_runs UNION ALL SELECT 'checkpoints', COUNT(*) FROM source_states UNION ALL SELECT 'baselines', COUNT(*) FROM source_ingestion_baselines; SELECT language, notify_direct, notify_potential, COUNT(*) AS users FROM users GROUP BY language, notify_direct, notify_potential; SELECT s.code, COUNT(*) AS publications FROM publications p JOIN sources s ON s.id=p.source_id GROUP BY s.code; SELECT status, COUNT(*) AS runs FROM ingestion_runs GROUP BY status;"
```

Keep both files outside Git and never upload them. If export, restore, integrity, permissions, or expected-table checks fail, stop.

### D. Apply only migration 0005

First list the remote migration state:

```sh
npx wrangler d1 migrations list denmarktgbot-db --remote
```

Proceed only if migrations 0001–0004 are applied and **0005 is the sole pending migration**. If any other migration is pending or the state is unexpected, stop. Then apply via Wrangler so its migration ledger is updated:

```sh
npx wrangler d1 migrations apply denmarktgbot-db --remote
npx wrangler d1 migrations list denmarktgbot-db --remote
```

Confirm 0005 is applied and no migration remains pending. Do not execute the SQL manually or apply any other migration. Verify the notification table constraint and migration ID:

```sh
npx wrangler d1 execute denmarktgbot-db --remote --command "SELECT sql FROM sqlite_master WHERE type='table' AND name='notifications'; SELECT name FROM d1_migrations ORDER BY name;"
```

Require the status constraint to include `SENDING`; compare the aggregate counts and user preference totals with step C. If any command or check is uncertain, stop. Do not restore the backup automatically.

### E. Deploy the reviewed Worker

Record the currently deployed version first using Wrangler's read-only deployment listing, then deploy the reviewed commit only:

```sh
cd /Users/alexandra/Projects/DenmarkTgBot/worker
npx wrangler deployments list --name denmarktgbot
npm run deploy
npx wrangler deployments list --name denmarktgbot
```

Record the previous version ID from the first listing in a local shell variable `PREVIOUS_VERSION`. Record the new version ID after deployment. If deployment fails, stop and use Wrangler's deployment rollback to restore the recorded Worker version: `npx wrangler rollback "$PREVIOUS_VERSION" --name denmarktgbot`. Do not change D1 or notification settings during rollback.

### F. Verify routes and existing bot behavior

Check public health and unauthenticated route protection:

```sh
curl -sS -o /dev/null -w 'health=%{http_code}\n' https://denmarktgbot.alexandra-strona.workers.dev/health
curl -sS -o /dev/null -w 'ingest=%{http_code}\n' -X POST https://denmarktgbot.alexandra-strona.workers.dev/internal/ingest
curl -sS -o /dev/null -w 'preview=%{http_code}\n' https://denmarktgbot.alexandra-strona.workers.dev/internal/notifications/preview
curl -sS -o /dev/null -w 'prepare=%{http_code}\n' -X POST https://denmarktgbot.alexandra-strona.workers.dev/internal/notifications/prepare
curl -sS -o /dev/null -w 'deliver=%{http_code}\n' -X POST https://denmarktgbot.alexandra-strona.workers.dev/internal/notifications/deliver
```

Require 200 for health and 401 for every unauthenticated internal route. Then use the existing local ingestion token for authenticated preview and read-only source-state GETs. Load it into a hidden prompt/environment; do not place it in command arguments, shell history, logs, or this report. Example for zsh:

```sh
read -s "INGEST_API_TOKEN?Worker ingestion token: "
printf '\n'
export INGEST_API_TOKEN
export WORKER_URL="https://denmarktgbot.alexandra-strona.workers.dev"
python3 - <<'PYCODE'
import os, urllib.error, urllib.request
base = os.environ['WORKER_URL']
token = os.environ['INGEST_API_TOKEN']
paths = [
    '/internal/source-states?source=hoeringsportalen',
    '/internal/source-states?source=folketinget',
    '/internal/notifications/preview',
]
for path in paths:
    request = urllib.request.Request(base + path, headers={
        'Authorization': 'Bearer ' + token,
        'User-Agent': 'DenmarkTgBot-IngestionClient/1.0',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            print(path.split('?')[0], response.status)
    except urllib.error.HTTPError as error:
        print(path.split('?')[0], error.code)
PYCODE
unset INGEST_API_TOKEN
```

Require HTTP 200 for the two source-state reads and preview; preview must contain aggregate fields only. Manually inspect Telegram navigation without sending synthetic updates. Verify the scheduled workflow remains paused at the gate.

### G. Verify notifications remain disabled and data unchanged

In Cloudflare Worker settings, verify the following runtime variables are absent or explicitly disabled; do not set them during this rollout:

- `NOTIFICATION_QUEUE_ENABLED`
- `NOTIFICATION_ACTIVATION_CUTOFF`
- `NOTIFICATION_DELIVERY_ENABLED`
- `NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF`

Confirm the three notification routes require authorization, and that an authenticated preview is read-only. Compare production notification count, publication/event counts, migration state, user preference aggregates, baselines and checkpoints against pre-migration records. Require notification count unchanged (expected 0 for the reviewed baseline) and existing source/user state unchanged. If any check differs unexpectedly, stop with the schedule gate still disabled.

### H. Resume scheduled ingestion

Only after steps A–G pass, restore the repository variable to its previously recorded value (recreate it only if it had been set before the pause; delete the temporary setting if it was originally absent). If it was exact lowercase `true`, scheduled collection resumes; if it was not, leave it disabled and ask the operator before enabling. Confirm a later scheduled run has both feed statuses successful and ingestion `COMMITTED`. Notification queueing and delivery remain disabled regardless of this variable.

## Rollback and data reconciliation

- **Before migration:** if a preflight fails, make no production changes and leave the automation gate disabled until understood.
- **Migration failure/uncertain state:** stop. Recheck Wrangler's migration ledger and read-only schema state. Do not rerun manually and do not restore the full database automatically.
- **Worker regression:** roll back the Worker deployment to the recorded previous version. Migration 0005 rebuilds the notifications table to extend its status constraint with `SENDING`; Worker rollback does not reverse that D1 schema migration. Verify the old Worker remains compatible before rollback. Leave scheduling disabled until health, webhook authentication, navigation and ingestion are reverified.
- **Unexpected data differences:** preserve the fresh backup, capture aggregate before/after counts and migration/run IDs, and reconcile affected rows deliberately. Full restore may discard legitimate writes made after backup and requires a separate explicit recovery decision.
- **Notifications:** do not turn either enable flag on as a rollback or diagnostic step. Delivery attempts in `SENDING` are ambiguous; never automatically resend them.
