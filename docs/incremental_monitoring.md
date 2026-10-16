# Høringsportalen incremental monitoring

## Current-window reconciliation

`python -m collectors.main` fetches both configured official ATOM feeds over HTTPS, validates them, preserves official IDs and original `updated` text, and merges duplicate IDs into one publication with membership in each matching feed. A failure, malformed entry, or invalid timestamp/cursor makes collection incomplete. The uploader refuses incomplete output before any ingestion request.

Every complete incremental run submits **all unique publications currently visible in both feeds**, including records whose timestamps are equal to or behind saved checkpoints. D1 upsert and semantic snapshot comparison decide whether a publication changed. Identical snapshots and source-timestamp-only changes create no events and do not refresh the publication's meaningful `updated_at` time. Title/content changes remain detectable even when the official `updated` timestamp does not advance. Historical mode, ruleset 1.1 classification, event idempotency, and bounded batch sizes remain in effect.

The two feed checkpoints remain independent. Each checkpoint records the greatest valid `(updated_at, official ID)` cursor observed in that feed and is proposed from that feed's successful window. A successful empty feed retains its prior cursor. Checkpoints no longer filter records: they record progress and are advanced only by the ingestion service's atomic successful commit. Failed or incomplete collection does not start a run; interrupted submission can be resumed with its original run ID.

## Feed-window limits

Each configured feed exposes only its latest 25 entries. Reconciliation prevents a visible older or unchanged-timestamp record from being silently skipped, but it cannot recover records that have already fallen out of both windows before a run. The source provides no archive pagination through these feeds. “Collection complete” means both current windows parsed cleanly; it does not mean exhaustive historical coverage. More frequent runs reduce, but do not eliminate, the chance that records age out between runs.

## First detection and notification policy

The ATOM `updated` timestamp does not establish when a publication first appeared. `published_at` is therefore not invented. A publication first observed by the bot is stored as `FIRST_SEEN` with `notify_eligible=0`, even in incremental mode. Historical records remain non-notifiable. Later meaningful updates to known records may be eligible under existing rules, but delivery remains disabled.

Future policy proposal: keep `FIRST_SEEN` ineligible by default. If product requirements call for alerts about newly observed items, define a separate, explicitly named “newly detected” notification policy, distinct from “newly published.” It should require user opt-in and describe the event as first detection by the bot. Only a trustworthy source publication date or a separately verified freshness signal could support describing it as newly published. Until then, no automatic first-seen alert should be sent.

## Cloudflare 403 / 1010 investigation

Earlier authenticated source-state reads returned HTTP 403 / Cloudflare Error 1010. In this investigation, the same two read-only GETs succeeded with HTTP 200 through both the current Python ingestion client (`DenmarkTgBot-IngestionClient/1.0`, JSON accept header) and system `curl` (diagnostic user agent). The responses contained Høringsportalen's two feed states and an empty Folketinget stream list. `curl` received `server: cloudflare`; the response ray IDs were `a4b792ec0ad2cbc0-MAD` (Høringsportalen) and `a4b792ef2ce2cfe6-MAD` (Folketinget). No 1010 error page appeared in these requests.

The earlier block is not reproducible now, so its precise trigger is unresolved. The Cloudflare dashboard redirected to its login page; no authenticated read-only WAF/security-rule view was available. No WAF setting was changed. The current successful GETs establish that the API is reachable now, but do not identify why Cloudflare previously returned 1010.

## Manual workflow and cost

`.github/workflows/hoeringsportalen-incremental.yml` remains `workflow_dispatch` only; there is no active `schedule` trigger. Manual runs offer `dry_run` (default) and `production_ingest`. Dry-run needs no confirmation, collects and validates both feeds, classifies/deduplicates records, then invokes the uploader's no-HTTP `--dry-run` path. Production ingestion requires `confirm_production_ingestion=true`; the endpoint/token are only passed to that step. A future scheduled event is separate from the manual selector and requires the repository Actions variable `HOERINGSPORTALEN_AUTOMATION_ENABLED` to be exactly `true`; missing or other values fail before collection. The default (unset) is disabled. The uploader's run ID is a UUID, and the Actions summary shows feed states/counts, collection completeness, relevance totals, run IDs, ingestion state and a generic error category; dry-run summaries explicitly say no production write occurred. Raw collector/uploader diagnostics are kept out of visible logs. The workflow uses Python's standard library, standard GitHub-hosted runners, a ten-minute timeout, and the existing non-cancelling concurrency lock. Configure `INGEST_API_URL` as a GitHub Actions variable and `INGEST_API_TOKEN` as a GitHub Actions secret before production ingestion. Neither variable activates scheduling.

To run a dry-run from the GitHub UI, open **Actions** → **Høringsportalen incremental collection** → **Run workflow**, leave `mode` as `dry_run`, and leave production confirmation unchecked. Review both feed statuses, `Collection complete: true`, relevance counts and `Ingestion: DRY_RUN` in the run summary. The workflow fails if collection is incomplete.

The concurrency group serializes runs of this GitHub Actions workflow only; it is not a lock for other API clients. The Worker remains responsible for idempotent staged batches, serialized database commit processing and atomic checkpoints. Local Worker integration tests cover concurrent commits and repeated ingestion; the workflow test verifies its non-cancelling lock remains configured.

## Future automatic pipeline (not enabled)

Recommended scheduler: GitHub Actions scheduled workflow for Høringsportalen, with the workflow file committed to the default branch and its current concurrency lock retained. The prepared future cadence is `17 6,14,22 * * *` UTC (06:17, 14:17, 22:17). This is only a proposal; no schedule trigger has been added. To activate later, an operator must explicitly review and commit the `schedule` trigger, configure `HOERINGSPORTALEN_AUTOMATION_ENABLED=true` in repository Actions variables, verify both ingest endpoint/token configuration, and manually observe a complete run with `COMMITTED` status. If the automation variable is unset or invalid, scheduled execution fails closed before collection and ingestion. A manual dispatch still requires its separate confirmation input.

The ten-minute job cap gives a maximum of 30 runner minutes/day, or 930 minutes in a 31-day month at three runs/day, before other workflows. This stays under the 2,000 standard-runner minute monthly allowance for GitHub Free private repositories; public standard runner use is free. Verify current repository visibility, account allowance and other workflows before activation. Scheduled runs are best effort and GitHub notes they can be delayed or dropped during high load, especially near the hour ([billing and included usage](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [schedule event limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)).

### Identifying failed or missed runs

Open the repository's **Actions** tab and select this workflow. A failed run is marked red; inspect the failed step names and the concise run summary for feed status, ingestion status, and a generic error category. The workflow exits nonzero if either feed collection fails/is incomplete, if upload fails, or if the server does not confirm `COMMITTED`. A scheduled run that is absent at an expected time may have been delayed or dropped; compare the latest successful run timestamp with the expected three daily UTC windows. Do not infer success from a workflow starting: verify `COMMITTED` and both feed statuses in the summary. Notifications remain independently controlled and are not queued or delivered by this workflow.

The intended end-to-end path is:

Official Høringsportalen feeds → collect both feeds → validate, deduplicate, and classify with ruleset 1.1 → submit complete bounded batches → Worker persists and compares semantic snapshots → eligible events enter the idempotent notification queue → separately gated Telegram delivery.

The local notification queue and delivery components are not called from the ingestion workflow. Automatic ingestion and notification delivery require independent activation controls. No notification queue is prepared, no Telegram delivery is attempted, and no schedule is active. After later approval, queue preparation may only follow a confirmed successful ingestion commit; delivery still needs a separate explicit enable flag/cutoff and review of ambiguous `SENDING` attempts.
