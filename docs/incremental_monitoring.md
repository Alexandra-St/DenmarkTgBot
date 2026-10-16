# Høringsportalen incremental monitoring

## Collection and submission

`python -m collectors.main` fetches both configured official ATOM feeds over HTTPS, validates and normalizes entries, merges duplicate official IDs, and writes JSON to stdout. A failed feed or invalid entry makes the collector exit nonzero. The uploader refuses incomplete output before making any ingestion request. It reads authenticated source state, classifies with deterministic ruleset 1.1, and submits bounded batches to the existing ingestion API.

Incremental upload compares each publication's `(updated_at, official ID)` cursor against the saved checkpoint for every feed containing that publication. A record is included if it is ahead of at least one corresponding feed checkpoint. An unset feed checkpoint includes that feed's whole current window. Exact-equal cursors are already observed. The two feed checkpoints advance independently from each feed's upper bound; an empty feed retains its prior checkpoint. A submission failure leaves checkpoint advancement to the ingestion service's commit transaction; retry an interrupted run with the same run ID.

The parser stores the normalized UTC update time and preserves the original ATOM `updated` string as `source_updated_at_raw` and in source metadata. Official IDs remain unchanged. Duplicate IDs merge feed membership and retain metadata from the newest entry.

## Feed limits and missed entries

Each configured feed returns only its latest 25 entries. Checkpoints therefore detect only entries still present in a feed window. If more than 25 publications or updates occur between successful runs, an entry may leave the window before collection. An old entry may also appear in a window after a long gap. The source provides no cursor pagination or archive feed in this collector. The client cannot prove that a window contains every entry since a checkpoint, cannot recover entries that have fallen out of both windows, and must not report complete historical coverage. Running more often reduces exposure but does not prove completeness; overlap by timestamp and ID protects boundary ordering only.

The feed's `updated` value is not proof of first publication time. A previously unseen old item appearing in a feed can be distinguished in storage as a `FIRST_SEEN` record, but its age cannot be established reliably from that timestamp. `published_at` remains unset. Høringsportalen `FIRST_SEEN` events are consequently stored with `notify_eligible=0`. Later meaningful changes to known records may be eligible under existing rules; no notification delivery is implemented or enabled. Technical timestamp-only changes do not create semantic change events.

## Failures and recovery

- If either feed is unavailable, malformed, or has invalid timestamps/cursor data, collection exits nonzero and the uploader makes no ingestion request.
- The uploader reads source state before filtering and submitting. Missing feed state causes that feed's visible entries to be included; an invalid saved checkpoint fails closed.
- Ingestion owns checkpoint compare-and-swap and commit behavior. Never advance checkpoints manually.
- If submission is interrupted, retain the run ID printed in the error and retry with `--run-id <same-id>`; inspect/abort an open run only through the documented ingestion API.
- A complete run can still have feed-window gaps. “Complete” means both current feeds parsed cleanly, not exhaustive source history.

## Future scheduling and cost

`.github/workflows/hoeringsportalen-incremental.yml` is manual `workflow_dispatch` only. There is no schedule. The job also requires the explicit confirmation input before it collects and submits. Configure `INGEST_API_URL` as a GitHub Actions variable and `INGEST_API_TOKEN` as a GitHub Actions secret; never store token values in the repository. The workflow has a ten-minute job timeout and concurrency lock. It uses Python's standard library and GitHub-hosted Linux runners, with no paid third-party services. Before enabling a schedule, choose an interval that fits the current 25-entry windows, verify GitHub Actions free-minute allowance for the account/repository, alert on failed runs, and review feed-gap risk. Scheduling and notifications remain disabled.

## Remaining risks

- No API or feed mechanism proves complete coverage across a gap longer than the current 25-entry windows.
- Same-ID corrections that do not advance `updated` cannot pass the cursor filter.
- A gap or delayed appearance cannot be reliably distinguished from a newly published item. First-seen notification eligibility is suppressed to avoid presenting it as breaking news.
- Production source-state reads must succeed before any incremental submission. Current preflight GETs were blocked at Cloudflare with HTTP 403 / Error 1010, so production checkpoints could not be independently verified in this task.
