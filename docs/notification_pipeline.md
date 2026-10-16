# Local notification pipeline

This phase adds a local-only pipeline. It is not connected to the webhook, a scheduled trigger, or production D1. Delivery is disabled unless a caller explicitly sets NOTIFICATION_DELIVERY_ENABLED=true; no current application route invokes it.

## Eligibility and queueing

prepareNotificationQueue creates PENDING rows from publication_events only when:

- NOTIFICATION_ACTIVATION_CUTOFF is a valid UTC ISO timestamp and has been reached;
- the event was detected strictly after that cutoff and within the last seven days;
- notify_eligible=1, the publication is complete and nonhistorical, and its relevance is DIRECT or POTENTIAL;
- the user opted in to that relevance category.

Høringsportalen FIRST_SEEN is explicitly excluded even if a row is accidentally marked eligible. An official updated_at timestamp is never used as evidence of publication freshness. Missing, malformed, or future cutoff values fail closed. Queue creation uses the existing (telegram_id, event_id) uniqueness and ON CONFLICT DO NOTHING, so repeated preparation does not create duplicate rows.

The activation cutoff is a manual future policy value, not a setting activated by this phase. Existing preferences are read only.

## Delivery and message content

The separate deliverPendingNotifications component uses a Telegram client interface so local tests use mocks. It rechecks the activation cutoff, freshness, event eligibility, publication history/completeness, category preference, and Høringsportalen first-seen restriction immediately before sending. It validates official HTTPS hosts and fails closed for unknown event types or invalid URLs.

Messages are localized in Ukrainian or Russian and include the relevance marker, change description, publication title, official source, official URL, and a legal-information disclaimer. Status changes include old and new statuses. The copy reports a first detection as “first detected by the bot,” never as newly published.

Migration 0005_notification_delivery_claim.sql adds SENDING. A dispatcher claims each pending row with a conditional SQLite update before calling Telegram; competing dispatchers cannot claim the same row. Successful sends become SENT. A returned error or timeout becomes terminal FAILED with a generic error code; credentials and Telegram response details are not stored.

## Retry and delivery limits

Telegram sendMessage and D1 status persistence cannot form one transaction. If Telegram accepts a message but the response times out, the bot cannot know whether delivery occurred. The pipeline does not retry failed sends automatically, avoiding a possible duplicate at the cost of a possibly missed notification. A process crash after claiming leaves SENDING for manual review; automatic lease recovery/retry is intentionally not implemented. Exactly-once delivery cannot be guaranteed by this design.

Before any future production activation, review the activation cutoff, migration state, categories, stale-event window, stuck SENDING recovery procedure, and an explicit manual delivery enablement. No scheduling or production delivery is enabled by this phase.
