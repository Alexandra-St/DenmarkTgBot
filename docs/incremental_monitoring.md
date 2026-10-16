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

`.github/workflows/hoeringsportalen-incremental.yml` is `workflow_dispatch` only, with an explicit production-ingestion confirmation input. No schedule or notification delivery is enabled. Configure `INGEST_API_URL` as a GitHub Actions variable and `INGEST_API_TOKEN` as a GitHub Actions secret. The workflow uses Python's standard library, GitHub-hosted runners, a ten-minute timeout, and a concurrency lock. Before considering a schedule, assess the 25-entry window risk, add failure alerting, and verify the repository's available GitHub Actions minutes. No paid service is required by this workflow.
