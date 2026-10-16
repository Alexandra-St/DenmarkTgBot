# One-recipient notification canary

The canary is a separate, single-use diagnostic route. It sends exactly `🧪 DenmarkBot — тестовое уведомление`; it does not read the notification queue or write publications/events/checkpoints. Its only D1 write is a singleton attempt record in migration 0006. The normal queue and delivery endpoints retain their existing gates and behavior. The canary never enables production-wide notification delivery.

## Required gates

The protected `POST /internal/notifications/canary` route requires the existing ingestion API bearer authorization. The Worker also requires all of:

- `NOTIFICATION_CANARY_ENABLED=true`;
- a valid, reached UTC `NOTIFICATION_CANARY_ACTIVATION_CUTOFF`;
- an explicit positive numeric `NOTIFICATION_CANARY_TELEGRAM_ID` allowlist value;
- a request whose `recipient_id` exactly matches that allowlist;
- request confirmation `SEND_ONE_CANARY_MESSAGE` and a UUID `request_id`;
- the existing Telegram bot token.

Missing or invalid configuration fails closed. The recipient is never inferred from users, preferences, recent activity, or a previous delivery. The workflow does not configure any of these values.

The route atomically claims the singleton D1 slot before contacting Telegram. Only one attempt is allowed per database. `SENT`, `FAILED`, or an interrupted `SENDING` state all block later calls. A timeout is ambiguous, so the canary is not retried automatically. This deliberately favors avoiding duplicates over convenient repeat attempts.

## First real canary procedure

No message has been sent as part of preparing this feature. Before a real test:

1. Obtain explicit approval for a single diagnostic message and the exact recipient. The recipient must consent, have started the bot, and provide their numeric Telegram ID through an approved private channel. Do not infer an ID from a username or copy it into logs/issues.
2. Review the exact release, local tests, migration 0006, and rollback plan. Back up production D1 and apply migration 0006 only through a separately approved production change. Deploy the reviewed Worker separately. This task has not done either operation.
3. After deployment checks pass, configure the canary allowlist ID, a valid activation cutoff, and `NOTIFICATION_CANARY_ENABLED=true` only for the approved test window. Keep `NOTIFICATION_QUEUE_ENABLED` and `NOTIFICATION_DELIVERY_ENABLED` at their independently reviewed values; the canary does not need to activate them.
4. A human with the required approval sends one authenticated `POST /internal/notifications/canary` request using the protected ingestion bearer token. Request JSON fields are `confirmation: "SEND_ONE_CANARY_MESSAGE"`, `request_id: <new UUID>`, and `recipient_id: <the approved numeric ID>`. Use a trusted local caller that reads the token from a secret store without printing it or placing it in command arguments. Do not trigger a GitHub Actions run for this endpoint.
5. Verify the recipient confirms receipt of the exact diagnostic text. Then inspect the canary row using a read-only query that omits the recipient ID and request ID:

   ```sql
   SELECT status, created_at, completed_at, last_error
   FROM notification_canary_attempts
   WHERE singleton_id = 1;
   ```

   `SENT` means Telegram returned success; recipient confirmation is the end-to-end check. `FAILED` means the attempt was not retried. `SENDING` means the process stopped after claiming; treat the outcome as ambiguous and do not resend.
6. Disable immediately by setting `NOTIFICATION_CANARY_ENABLED` away from exact `true`, then remove the allowlist and cutoff. Keep the singleton audit row. Do not reset it to run another canary; any later test needs a separately reviewed design and explicit approval.

## Error handling and scope

- `401`: bearer authorization failed; do not include credentials when reporting it.
- `blocked`: a required gate, cutoff, allowlist, explicit confirmation, recipient match, or Telegram configuration is missing/invalid.
- `already_used`: an attempt was previously claimed; do not retry.
- `failed`: Telegram returned an error or timed out after the claim; outcome may be ambiguous and is terminal.
- `sent`: Telegram accepted the API request; confirm receipt with the approved recipient.

The response and logs must not include Telegram IDs, tokens, or message payloads. This diagnostic message cannot appear in News or Archive because it creates no publication/event records. It does not touch source checkpoints or regular notification queue rows.
