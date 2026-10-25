# Controlled PENDING notification reconciliation

This maintenance operation terminalizes existing notifications that are already
ineligible because of **current publication relevance or recipient preference**.
It does not reclassify publications, change ingestion/source state, create events,
queue notifications, or deliver messages. No migration is needed.

## Endpoints and independent gates

Authenticated with the existing ingestion bearer token:

- `POST /internal/notifications/reconcile/preview` — read-only, including with write gates absent.
- `POST /internal/notifications/reconcile/apply` — controlled writes only.

Apply requires all of:

- `PENDING_RECONCILIATION_WRITE_ENABLED` exactly `true`;
- `NOTIFICATION_DELIVERY_ENABLED` absent or not exactly `true`;
- `PENDING_RECONCILIATION_PLAN_SHA256` equal to the server canonical plan hash;
- `PENDING_RECONCILIATION_EXPECTED_COUNT` equal to the number of entries;
- fixed confirmation `APPLY_PINNED_PENDING_RECONCILIATION`.

Reclassification gates/pins cannot authorize this operation. None of these
variables are configured by the code or workflow. Deployment and activation
require a separately approved operational phase.

## Exact plan schema

The root has only `entries`, a nonempty array with at most 1000 entries. Every
entry has exactly these six fields:

```json
{
  "notification_id": 10,
  "event_id": 20,
  "source": "folketinget",
  "external_id": "12345",
  "expected_status": "PENDING",
  "expected_reconciliation_reason": "reclassified_irrelevant"
}
```

IDs are positive JavaScript safe integers; source is `folketinget` or
`hoeringsportalen`; external_id is a positive decimal string of at most19 digits.
Unknown fields, duplicate notification IDs, Telegram IDs, arbitrary reasons and
SQL are rejected. The only reasons are:

- `reclassified_irrelevant`: current publication relevance is IRRELEVANT;
- `reclassified_not_eligible`: current relevance is DIRECT/POTENTIAL and the
  corresponding current user preference is off.

Age, cutoff, historical/deleted/incomplete flags and event eligibility alone
cannot add an entry. These out-of-scope rows stay untouched. A missing current
user/event/publication/source relation cannot be READY.

The request body is `{"plan": <exact plan>}` for preview; apply additionally
contains the fixed `confirmation`. The server request limit remains1 MiB; the
manual workflow/local generator limit is61,440 UTF-8 bytes. No automatic split.

## Read-only local plan generation

Obtain a **fresh, authorized read-only** export first. The generator accepts a
JSON array of rows, `{"notifications": [...]}`, or the JSON result of a single
Wrangler SELECT. The export must include all PENDING rows. The following SQL
selects current identity/preferences without exporting Telegram IDs:

```sql
SELECT n.id AS notification_id,n.event_id,n.status,n.last_error,
       s.code AS source,p.external_id,p.relevance,
       CASE WHEN u.telegram_id IS NULL THEN 0 ELSE 1 END AS recipient_exists,
       u.notify_direct,u.notify_potential,
       p.source_complete,p.source_deleted,p.historical,
       e.notify_eligible,e.detected_at,e.event_type
FROM notifications n
LEFT JOIN publication_events e ON e.id=n.event_id
LEFT JOIN publications p ON p.id=e.publication_id
LEFT JOIN sources s ON s.id=p.source_id
LEFT JOIN users u ON u.telegram_id=n.telegram_id
WHERE n.status='PENDING'
ORDER BY n.id;
```

Keep the export/plan outside the repository and private. With that existing
local export, run from the repository root:

```bash
umask 077
python3 -m tools.pending_reconciliation_plan \
  --audit-json /private/path/pending-audit.json \
  --output /private/path/pending-reconciliation-plan.json
```

This command has no HTTP or D1 operations. It scans PENDING rows and includes
only the two allowed reasons; terminal and other out-of-scope rows are skipped.
It writes an exclusive0600 file (never overwrites a reviewed plan) and prints
aggregate counts/size only. A malformed/duplicate audit, empty selection or
exceeded limit fails closed. Review the exact generated plan before submission.

## Preview, atomic predicates and races

Preview returns aggregate counts/hash and one ordinal-index state per entry:
READY, ALREADY_RECONCILED, STALE, MISSING or INVALID. It does not expose recipient
IDs, source content, or raw database errors. READY requires exact notification,
event and source/external publication identity, existing relations, no active
source ingestion and a recomputed current reason matching the plan.

Apply inspects the **whole plan before writes**. Any initial STALE/MISSING/INVALID
blocks the invocation with writes=0. Each READY row then uses one atomic UPDATE:

- identity and `status='PENDING'` must still match;
- a correlated SQL predicate rechecks current relevance and preference and
  the user/event/publication/source joins;
- active OPEN/COMMITTING ingestion for that source blocks the update;
- only `status` and `last_error` are updated.

No publication timestamp/hash CAS is needed for unrelated content: the atomic
condition proves the exact current suppression reason itself. A relevance or
preference change that makes the row eligible, an identity change, an ingestion
start or a PENDING→SENDING claim causes changes=0 and a non-success response.

The operation is **not a global transaction across rows**. A late race can leave
earlier acknowledged rows reconciled: the response is409 CONFLICT/PARTIAL and
processing stops. A storage/transport failure is503 INDETERMINATE; `writes`
counts acknowledged updates only and must not be read as proof of rollback or
zero actual writes. No automatic retries. Inspect production read-only before
deciding whether the exact same plan may be replayed.

SENT, SENDING and unrelated FAILED rows are immutable. attempts, sent_at,
created_at, telegram_id and event_id are never modified. Exact FAILED+reason
with matching identity is ALREADY_RECONCILED; a repeated successful plan has
ready=0, already_reconciled=count, writes=0, even if preferences later change.

## Manual workflow and durable audit

Workflow: **Controlled pending reconciliation**, file
`.github/workflows/pending-reconciliation-maintenance.yml`.
Only workflow_dispatch; inputs are action (preview/apply), plan_json and
confirm (default false). Apply requires confirm exactly true in the helper.

The exact plan is read from GITHUB_EVENT_PATH, never step env or shell
interpolation. The shared transport preserves raw plan bytes, disables
redirects, uses a fixed User-Agent, a20-second timeout, a bounded response,
private0600 temporary files and cleanup. It performs one request, without retry,
and filters output to aggregate counts/status/server hash. A non-2xx fails.
URL/token come only from existing repository variable/secret. The workflow
shares the publication-maintenance concurrency group to serialize maintenance,
while its server gates and pins remain independent.

Minimum audit trail: retained FAILED notification rows with deterministic
last_error; private immutable exact plan; pinned server SHA/count; and the
authenticated GitHub maintenance run timestamp/aggregate result and read-only
before/after verification. Retain those operator artifacts durably. No fake
RECLASSIFIED publication events or additional audit table are created.

Regression fixtures cover the former event1743 PENDING pair and the unrelated
event1722 SENT record; implementation contains no source/event-specific IDs.
No production reconciliation or preview is part of this implementation phase.
