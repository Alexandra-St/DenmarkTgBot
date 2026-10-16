# Phase 1.26 — Beta release preflight

**Result: BLOCKED for deployment.** Local tests pass and read-only production data checks match the expected publication state. Do not deploy until the active notification/canary settings and deployed source revision are confirmed.

## Verified on 2026-10-16

- Worker `denmarktgbot` responds to `GET /health` with HTTP 200 and `{ "status": "ok", "service": "DenmarkTgBot" }`.
- Latest Cloudflare deployment shown by Wrangler: `7889a0b6-efa5-45a0-93b4-4bf27d99564e` (100%), created 2026-10-16 18:58 UTC. Cloudflare reports its source as `Unknown`; it does not provide a Git SHA. The exact source diff since the deployed Worker therefore cannot be established from deployment history.
- Remote D1 `denmarktgbot-db` has no pending migrations. The repository migrations through `0006_notification_canary_once.sql` are applied.
- Aggregate D1 counts: 2 users, 25 Høringsportalen publications, 0 Folketinget publications, 0 notifications. Three ingestion runs are `COMMITTED`; Høringsportalen has two stream checkpoints and one baseline; Folketinget has no checkpoints or baseline.
- User preference aggregates: one Russian user with DIRECT on/POTENTIAL off, and one Ukrainian user with both categories on. The previously recorded Russian preference group remains; no user-level comparison was made because this check intentionally used aggregates only.
- Both Høringsportalen stream timestamps are `2026-10-16T17:16:44.370Z`. They are within the UI's 24-hour freshness window, so About should show the source as active. Folketinget has no source rows and is presented as inactive.
- No production writes, deployment, Telegram sends, or configuration changes were made during this preflight.

## Code and test review

- Onboarding and both language choices are covered in Worker tests. Language selection persists defaults and repeated `/start` does not duplicate a user.
- News and Archive use the existing D1 publication repository; navigation, filters, pagination, detail opening/return, and localization have regression coverage.
- Notification preference controls remain per-category and idempotent.
- About reads the conservative earlier timestamp of both Høringsportalen streams, renders active/stale/unknown without inventing a date, and explicitly says Folketinget monitoring is inactive. Freshness is active for at most 24 hours; future or malformed timestamps become unknown.
- Notification queue, delivery, and canary code have independent fail-closed gates. However, code defaults alone do not verify deployed runtime values. Recent Cloudflare deployment entries show canary variables being added, changed, and removed. Their current values cannot be established from deployment history output, so disabled delivery and disabled canary are **not verified**.
- Folketinget incremental ingestion is blocked by the absent historical baseline. A baseline cannot safely be created until the ODA collector can complete a reliable scan; the known cursor-pagination issue remains fail-closed.
- Source-status changes query existing `source_states`; no new schema is required. There are no pending production migrations.

Validation:

- `python3 -m unittest discover -s tests -v` — 156 passed.
- `npm run typecheck` — passed.
- `npm test` — 53 passed.

## Blockers before deployment

1. In Cloudflare, verify without changing anything that `NOTIFICATION_QUEUE_ENABLED`, `NOTIFICATION_DELIVERY_ENABLED`, and `NOTIFICATION_CANARY_ENABLED` are not exact lowercase `true`; verify their activation cutoffs and canary recipient configuration do not permit delivery. Do not copy values into logs or this repository. If a gate is enabled or its state is unclear, stop and obtain an explicit operational decision.
2. Identify the Git commit represented by deployed version `7889a0b6-efa5-45a0-93b4-4bf27d99564e` from the deployment record or the operator who deployed it. The Wrangler deployment listing currently says `Source: Unknown`; do not assume that latest `main` equals production.
3. Confirm the beta audience and that notification/canary routes will not be invoked during beta. No production user message should be used as a diagnostic.

## Safe deployment checklist (for a separately approved deployment)

1. In the normal project checkout, confirm `main` is current and clean except for the preserved untracked `scripts/` directory. Record `git rev-parse HEAD` and the current Cloudflare version ID.
2. Verify the three notification/canary enable gates and related cutoffs in Cloudflare. Require delivery and canary to be disabled. Stop on any uncertainty. Do not change environment values as part of this checklist.
3. Confirm the remote migration list has no pending migrations and recheck the D1 aggregate counts and preference groups above. Do not run migration commands that apply changes.
4. Run:

   ```sh
   python3 -m unittest discover -s tests -v
   cd worker
   npm run typecheck
   npm test
   npm run deploy
   ```

5. Record the new Worker version. Verify `GET /health` is HTTP 200, unauthenticated `/internal/ingest`, `/internal/source-states`, and notification routes reject access, and `/telegram/webhook` still rejects missing authentication. Do not send synthetic Telegram updates.
6. Repeat read-only D1 aggregate checks: users and preference groups, 25/0 source publication counts, zero notifications, two Høringsportalen checkpoints, Høringsportalen baseline, and no Folketinget baseline. Confirm migration state unchanged.
7. If any route or data check regresses, stop beta testing and roll back the Worker to the recorded version. Do not restore D1 or change secrets/webhook settings as an automatic rollback.

## Private beta checklist for two users

Use two consenting test accounts only; avoid publishing account identifiers in notes.

1. User A selects Ukrainian and User B selects Russian on first `/start`; verify each sees the matching main menu and a repeat `/start` keeps the selected language.
2. In both languages, open News and Archive; check source-update ordering, disclaimer text, DIRECT/POTENTIAL filters, next/previous navigation, a publication card, and return to the same list/filter/page.
3. Open About and verify Høringsportalen shows the same last successful check time (localized to Danish time) and active status, while Folketinget is clearly inactive. Neither view should imply continuous monitoring or complete Folketinget coverage.
4. Open notification settings for both users. Toggle DIRECT and POTENTIAL independently, revisit the screen, and confirm settings persist. No real notification or canary message should arrive during these checks.
5. If either account receives an unexpected notification, stop testing and report it; do not repeat the action to investigate.
