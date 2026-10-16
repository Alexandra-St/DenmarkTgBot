# Local notification pipeline

The notification pipeline is connected to protected internal Worker routes and scheduled workflow orchestration code, but remains disabled by default. This phase changed local code only: no production configuration or database was changed, no workflow was triggered, and no Telegram message was sent.

## Eligibility and queueing

The authenticated `POST /internal/notifications/prepare` route accepts a run ID and creates PENDING rows only for events whose keys belong to that specific ingestion run. It first verifies that D1 records the run as `COMMITTED` and `incremental`; historical, open, blocked, and unrelated runs cannot create queue rows. It also requires `NOTIFICATION_QUEUE_ENABLED=true`. The queue function then creates PENDING rows only when:

- NOTIFICATION_ACTIVATION_CUTOFF is a valid UTC ISO timestamp and has been reached;
- the event was detected strictly after that cutoff and within the last seven days;
- notify_eligible=1, the publication is complete and nonhistorical, and its relevance is DIRECT or POTENTIAL;
- the user opted in to that relevance category.

Høringsportalen FIRST_SEEN is explicitly excluded even if a row is accidentally marked eligible. An official updated_at timestamp is never used as evidence of publication freshness. Missing, malformed, or future cutoff values fail closed. Queue creation uses the existing (telegram_id, event_id) uniqueness and ON CONFLICT DO NOTHING, so repeated preparation does not create duplicate rows.

`NOTIFICATION_ACTIVATION_CUTOFF` is the queue activation cutoff. Queue creation is disabled unless its separate enable flag is exactly `true` and the cutoff is valid and reached. Missing, malformed, or future values fail closed. Existing preferences are read only.

## Local integration verification

The end-to-end test uses a disposable file-backed SQLite database with the D1 schema and all migrations. It runs the real ingestion handler, prepares records with the existing Python ruleset 1.1 ingestion client, imports 25 historical Høringsportalen records, then submits a complete incremental window with two changed publications. The test verifies the baseline and first-seen suppression, detects the title/content events, queues only matching DIRECT/POTENTIAL user preferences in Ukrainian/Russian, and completes mock-only deliveries. A repeated identical window creates no events and no additional deliveries. No external source or Telegram request is made. Python orchestration tests mock both Worker routes and verify the actual committed run ID is used.

Migration 0005 is also tested from an empty database and against the previous schema with existing user preferences, publication, event, and PENDING notification rows; those rows and values remain intact.

## Delivery and message content

The authenticated `POST /internal/notifications/deliver` route uses the existing bounded-timeout Telegram API client. It requires `NOTIFICATION_DELIVERY_ENABLED=true` and a distinct valid `NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF`; both are absent by default. The delivery component rechecks freshness, event eligibility, publication history/completeness, category preference, and Høringsportalen first-seen restriction immediately before sending. It validates official HTTPS hosts and fails closed for unknown event types or invalid URLs.

Messages are localized in Ukrainian or Russian and include the relevance marker, change description, publication title, official source, official URL, and a legal-information disclaimer. Status changes include old and new statuses. The copy reports a first detection as “first detected by the bot,” never as newly published.

Migration 0005_notification_delivery_claim.sql adds SENDING. A dispatcher claims each pending row with a conditional SQLite update before calling Telegram; competing dispatchers cannot claim the same row. Successful sends become SENT. A returned error or timeout becomes terminal FAILED with a generic error code; credentials and Telegram response details are not stored.

## Retry and delivery limits

Telegram sendMessage and D1 status persistence cannot form one transaction. If Telegram accepts a message but the response times out, the bot cannot know whether delivery occurred. The pipeline does not retry failed sends automatically, avoiding a possible duplicate at the cost of a possibly missed notification. A process crash after claiming leaves SENDING for manual review; automatic lease recovery/retry is intentionally not implemented. Exactly-once delivery cannot be guaranteed by this design.

## Future monitoring sequence

The scheduled sequence is: official Høringsportalen feeds → validate both feeds → bounded incremental ingestion → confirm `COMMITTED` and `checkpoints_advanced` → prepare the queue with the actual returned ingestion run ID → optionally deliver. Manual `dry_run` and `production_ingest` modes never invoke notification routes. Queue preparation requires GitHub variable `NOTIFICATION_QUEUE_AUTOMATION_ENABLED=true`; delivery additionally requires `NOTIFICATION_DELIVERY_AUTOMATION_ENABLED=true`. Missing or non-exact values are disabled. Queue errors or an ineligible Worker response prevent delivery.

These GitHub variables only permit workflow actions. The Worker independently requires `NOTIFICATION_QUEUE_ENABLED=true` and a valid reached `NOTIFICATION_ACTIVATION_CUTOFF`; delivery separately requires `NOTIFICATION_DELIVERY_ENABLED=true` and a valid reached `NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF`. All four Worker settings must be deliberately configured; repository code never sets them. The orchestration client makes one request per endpoint and never retries delivery.

## Staged activation and pause

1. Keep both GitHub notification variables unset or set to a value other than exact `true` until reviewed. Existing collection scheduling remains independently controlled by `HOERINGSPORTALEN_AUTOMATION_ENABLED`.
2. To permit queueing, set only `NOTIFICATION_QUEUE_AUTOMATION_ENABLED=true` and separately configure the Worker queue flag and cutoff. Otherwise queue preparation remains closed.
3. Enable delivery later as a separate decision: set `NOTIFICATION_DELIVERY_AUTOMATION_ENABLED=true` and separately configure the Worker delivery flag and cutoff.
4. Pause queueing or delivery by changing its GitHub variable away from exact `true`; each Worker flag can also close its corresponding operation. Existing PENDING rows remain stored.
5. Inspect the Actions summary for aggregate queue and delivery counts. It omits identities. Worker/D1 state remains the authoritative audit.

`PENDING` means awaiting a permitted delivery attempt; `FAILED` is terminal, including ambiguous Telegram timeouts. `SENDING` indicates a claim before interruption. Do not automatically reset it; investigate through an approved operational process. Telegram acceptance and D1 status persistence are not one transaction, so exactly-once delivery is not guaranteed.

The GitHub Actions workflow already has a three-run daily schedule at minute 17 (17 6,14,22 * * * UTC), protected by `HOERINGSPORTALEN_AUTOMATION_ENABLED`. The current repository variable value is not verified here; the schedule may reach collection only when that value is exact lowercase `true`. Keep the existing concurrency group and ten-minute timeout. Three maximum-length runs per day would use up to 930 minutes in a 31-day month. GitHub Free currently includes 2,000 standard-runner minutes monthly for private repositories, while standard hosted runners are free for public repositories; verify account usage and budget before relying on scheduled monitoring. GitHub notes scheduled runs may be delayed or dropped under high load, especially near the hour ([billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [schedule limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)).


`GET /internal/notifications/preview` is authenticated with the existing ingestion bearer token. It returns aggregate event/recipient counts by category and historical/stale suppression counts; it never returns identities or writes queue/status rows. With missing or invalid queue activation cutoff it returns zero eligible counts and `activation_configured: false`, while still reporting aggregate historical/stale suppression counts.

Migration 0005 adds the `SENDING` claim state and has local compatibility coverage from both empty schema and preexisting records. It has not been applied to production by this phase. Before any future production activation, review the migration state, activation cutoffs, categories, stale-event window, stuck SENDING recovery procedure, and separate queue/delivery enable flags. No scheduling or production delivery is enabled by this phase.
