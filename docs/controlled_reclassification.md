# Controlled relevance maintenance

This maintenance API does not perform ingestion, advance source state, or send Telegram messages. It uses the existing ingestion bearer authorization. No migration is required. All runtime write settings default to disabled.

## Plan and pin

`POST /internal/reclassification/preview` accepts `{"plan":{"entries":[...]}}`. Each entry has exactly:

- `source`: `folketinget` or `hoeringsportalen`
- `external_id`: official positive integer ID as a string
- `expected_relevance`: `DIRECT`, `POTENTIAL`, or `IRRELEVANT`
- `expected_ruleset_version`: current version string or null
- `expected_updated_at`: exact stored timestamp
- `expected_normalized_snapshot_sha256`: SHA-256 of the exact stored `normalized_snapshot_json` UTF-8 bytes, including formatting
- `new_relevance`, `new_relevance_reason`, `new_ruleset_version`

The plan is bounded to 1,000 unique publications and a 1 MiB request. Unknown fields and duplicate identities fail closed. No-op entries are invalid. Preview is read-only and returns counts for READY, ALREADY_APPLIED, STALE, MISSING and INVALID. Conflict details contain only validated source/external ID/state, never user identities or source content.

The server returns `plan_sha256`: SHA-256 of the canonical **plan object**, excluding the request envelope/confirmation. Canonicalization recursively sorts object keys, preserves array order and JSON types, and uses JavaScript JSON string escaping with UTF-8 encoding and no formatting whitespace (`canonicalPlan` in `worker/src/reclassification.ts`). Array order is part of the pin.

## Controlled procedure

1. Build a **fresh production plan immediately before the write** from a new read-only export and the approved classifier. Old audit exports are insufficient.
2. Preview and independently review all entries and aggregate impact.
3. Keep Worker `NOTIFICATION_DELIVERY_ENABLED` off; stop overlapping ingestion and drain any in-flight delivery requests. Do not enable delivery during maintenance. Verify there are no SENDING notifications or OPEN/COMMITTING ingestion runs for affected sources.
4. Pin `RECLASSIFICATION_PLAN_SHA256` and decimal `RECLASSIFICATION_EXPECTED_COUNT`; explicitly set `RECLASSIFICATION_WRITE_ENABLED=true` only for the approved operation.
5. Call `POST /internal/reclassification/apply` with the **same plan** and `confirmation: "APPLY_PINNED_RECLASSIFICATION_PLAN"`.
6. Verify aggregate counts, stored classification, one audit event per applied entry, notification terminalization, and unchanged source state. Disable the write gate after verification, including after failure.

This phase implements the mechanism only. It does not create a production plan, configure gates, deploy, or apply changes.

## Atomicity, CAS and recovery

The entire plan is inspected before any write. STALE/MISSING/INVALID blocks that invocation without writes. Each subsequent publication uses a separate transactional D1 `batch`: publication CAS update, conditional maintenance event insertion, conditional PENDING suppression. SQL `changes()` ties the latter two statements to the preceding mutation in that batch. A statement failure rolls back that publication's whole transaction.

CAS compares source ID + external ID, relevance, nullable ruleset, exact updated_at, exact stored snapshot text (after checking its SHA), and the fetched nullable reason. It also requires complete/non-deleted state, no existing operation event, no related SENDING notification, and no active source ingestion run. Source fields, source timestamps, historical flag and relationship data are never rewritten. Only relevance is changed inside the normalized snapshot: that is the classification field consumed/stored by current ingestion. Reason/ruleset are updated in their existing publication columns.

There is **no global transaction** across the plan. After a later conflict the loop stops; response is non-success HTTP 409 `PARTIAL`, with applied/already_applied/stale/missing/invalid/blocked counts and `ready` counting unattempted entries. Earlier batches remain applied. HTTP 503 `INDETERMINATE` means a storage/transport failure could have occurred after a commit: returned counters are acknowledged results, not proof that the failing batch rolled back. Read-only preview reconciles the exact same pinned plan before any retry; do not invent a replacement operation to bypass conflicts.

A retry is ALREADY_APPLIED only when its deterministic operation event, current relevance/reason/ruleset, post-snapshot hash and maintenance timestamp all agree. Subsequent source mutations invalidate that proof. Concurrent callers cannot duplicate the same event. A normal equivalent ingestion compares the updated snapshot relevance and creates no duplicate RECLASSIFIED event.

## Audit and notifications

Maintenance event keys use `reclassification:<plan hash>:<source/external ID hash>`, never the `ingest:` namespace. Each actual update creates exactly one RECLASSIFIED event with `notify_eligible=0`. Its bounded change JSON stores old/new relevance, old/new ruleset, fixed reason `controlled_reclassification`, and post-snapshot hash. These events never enter the publication notification queue.

For IRRELEVANT all related PENDING rows become FAILED with `last_error=reclassified_irrelevant`. DIRECT/POTENTIAL transitions terminalize only rows excluded by current user preferences, with `last_error=reclassified_not_eligible`. Attempts and sent_at are preserved. Eligible PENDING stays pending. SENT and FAILED history is immutable; SENDING is never reset and blocks the publication transaction.

The delivery gate is mandatory because Telegram sends cannot be rolled back. If an old delivery selected a pending row before suppression, its PENDING-to-SENDING claim loses after suppression; if it claimed first, SENDING blocks apply. Delivery must remain off and old invocations must be drained throughout the operation. The maintenance API does not change delivery gates, notification automation or ordinary delivery semantics.
