import { isIngestionRequestAuthorized } from "./ingestion.ts";
import { canonicalPlan, sha256 } from "./reclassification.ts";

export interface PendingReconciliationEnv {
  DB: D1Database;
  INGEST_API_TOKEN?: string;
  NOTIFICATION_DELIVERY_ENABLED?: string;
  PENDING_RECONCILIATION_WRITE_ENABLED?: string;
  PENDING_RECONCILIATION_PLAN_SHA256?: string;
  PENDING_RECONCILIATION_EXPECTED_COUNT?: string;
}
type Reason = "reclassified_irrelevant" | "reclassified_not_eligible";
type State = "READY" | "ALREADY_RECONCILED" | "STALE" | "MISSING" | "INVALID";
interface Entry {
  notification_id: number; event_id: number; source: string; external_id: string;
  expected_status: "PENDING"; expected_reconciliation_reason: Reason;
}
interface Row {
  status: string; last_error: string | null; event_id: number;
  related_event: number | null; publication_id: number | null;
  source: string | null; external_id: string | null; relevance: string | null;
  recipient_exists: number; notify_direct: number | null; notify_potential: number | null;
  active_ingestion: number;
}
const keys = ["notification_id", "event_id", "source", "external_id", "expected_status", "expected_reconciliation_reason"];
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const id = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v > 0;
const hash = (v: unknown): v is string => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const json = (v: unknown, status = 200) => new Response(JSON.stringify(v), {
  status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
});
function valid(v: unknown): v is Entry {
  return object(v) && Object.keys(v).length === keys.length && keys.every(k => Object.hasOwn(v, k))
    && id(v.notification_id) && id(v.event_id)
    && ["folketinget", "hoeringsportalen"].includes(v.source as string)
    && typeof v.external_id === "string" && /^[1-9][0-9]{0,18}$/.test(v.external_id)
    && v.expected_status === "PENDING"
    && ["reclassified_irrelevant", "reclassified_not_eligible"].includes(v.expected_reconciliation_reason as string);
}
async function readBody(request: Request): Promise<unknown> {
  if (!request.body) throw new Error();
  const reader = request.body.getReader(); const chunks: Uint8Array[] = []; let size = 0;
  while (true) {
    const part = await reader.read(); if (part.done) break;
    size += part.value.byteLength;
    if (size > 1024 * 1024) { await reader.cancel(); throw new Error(); }
    chunks.push(part.value);
  }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}

async function inspect(db: D1Database, entry: Entry): Promise<State> {
  // Recipient identity stays in D1. Only preference booleans are read internally.
  const row = await db.prepare(`SELECT n.status,n.last_error,n.event_id,e.id AS related_event,
    p.id AS publication_id,s.code AS source,p.external_id,p.relevance,
    CASE WHEN u.telegram_id IS NULL THEN 0 ELSE 1 END AS recipient_exists,
    u.notify_direct,u.notify_potential,
    EXISTS(SELECT 1 FROM ingestion_runs r WHERE r.source_id=p.source_id AND r.status IN ('OPEN','COMMITTING')) AS active_ingestion
    FROM notifications n LEFT JOIN publication_events e ON e.id=n.event_id
    LEFT JOIN publications p ON p.id=e.publication_id LEFT JOIN sources s ON s.id=p.source_id
    LEFT JOIN users u ON u.telegram_id=n.telegram_id WHERE n.id=?`).bind(entry.notification_id).first<Row>();
  if (!row) return "MISSING";
  if (!row.related_event || !row.publication_id || row.event_id !== entry.event_id
      || row.source !== entry.source || row.external_id !== entry.external_id) return "STALE";
  if (row.status === "FAILED" && row.last_error === entry.expected_reconciliation_reason) return "ALREADY_RECONCILED";
  if (row.status !== "PENDING" || row.recipient_exists !== 1 || row.active_ingestion) return "STALE";
  if (![0, 1].includes(row.notify_direct as number) || ![0, 1].includes(row.notify_potential as number)
      || !["DIRECT", "POTENTIAL", "IRRELEVANT"].includes(row.relevance as string)) return "INVALID";
  // Age/cutoff/history/deletion/completeness alone never justify this operation.
  const reason: Reason | null = row.relevance === "IRRELEVANT" ? "reclassified_irrelevant"
    : ((row.relevance === "DIRECT" ? row.notify_direct : row.notify_potential) === 0 ? "reclassified_not_eligible" : null);
  return reason === entry.expected_reconciliation_reason ? "READY" : "STALE";
}

export async function handlePendingReconciliationRequest(request: Request, env: PendingReconciliationEnv): Promise<Response> {
  if (!await isIngestionRequestAuthorized(request, env)) return json({ error: "unauthorized" }, 401);
  const path = new URL(request.url).pathname;
  if (!["/internal/notifications/reconcile/preview", "/internal/notifications/reconcile/apply"].includes(path))
    return json({ error: "not_found" }, 404);
  if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
  const apply = path.endsWith("/apply");
  if (apply && (env.PENDING_RECONCILIATION_WRITE_ENABLED !== "true" || env.NOTIFICATION_DELIVERY_ENABLED === "true"))
    return json({ error: "pending_reconciliation_blocked", writes: 0 }, 409);
  let body: unknown;
  try { body = await readBody(request); } catch { return json({ error: "invalid_plan", writes: 0 }, 400); }
  if (!object(body) || Object.keys(body).some(k => !["plan", ...(apply ? ["confirmation"] : [])].includes(k))
      || !object(body.plan) || Object.keys(body.plan).length !== 1 || !Array.isArray(body.plan.entries)
      || body.plan.entries.length < 1 || body.plan.entries.length > 1000) return json({ error: "invalid_plan", writes: 0 }, 400);
  if (apply && body.confirmation !== "APPLY_PINNED_PENDING_RECONCILIATION") return json({ error: "invalid_confirmation", writes: 0 }, 400);
  const entries = body.plan.entries; const digest = await sha256(canonicalPlan(body.plan));
  if (apply && (!hash(env.PENDING_RECONCILIATION_PLAN_SHA256) || env.PENDING_RECONCILIATION_PLAN_SHA256 !== digest))
    return json({ error: "plan_hash_mismatch", writes: 0 }, 409);
  if (apply && (!/^[1-9][0-9]{0,3}$/.test(env.PENDING_RECONCILIATION_EXPECTED_COUNT ?? "")
      || Number(env.PENDING_RECONCILIATION_EXPECTED_COUNT) !== entries.length)) return json({ error: "plan_count_mismatch", writes: 0 }, 409);
  const counts = { ready: 0, applied: 0, already_reconciled: 0, stale: 0, missing: 0, invalid: 0, blocked: 0,
    writes: 0, irrelevant_count: 0, preference_count: 0 };
  const results: Array<{ index: number; state: State; reason?: Reason }> = [];
  const items: Array<{ entry: Entry; index: number; state: State }> = [];
  const seen = new Set<number>();
  const response = (status: string, http = 200) => json({ status, plan_sha256: digest, expected_count: entries.length, ...counts, entries: results }, http);
  try {
    // Inspect the entire plan before writes. Any initial conflict blocks everything.
    for (const [index, raw] of entries.entries()) {
      if (!valid(raw) || seen.has(raw.notification_id)) { counts.invalid++; results.push({ index, state: "INVALID" }); continue; }
      seen.add(raw.notification_id);
      const state = await inspect(env.DB, raw); items.push({ entry: raw, index, state });
      results.push({ index, state, reason: raw.expected_reconciliation_reason });
      if (state === "READY") {
        counts.ready++;
        counts[raw.expected_reconciliation_reason === "reclassified_irrelevant" ? "irrelevant_count" : "preference_count"]++;
      } else if (state === "ALREADY_RECONCILED") counts.already_reconciled++;
      else counts[state.toLowerCase() as "stale" | "missing" | "invalid"]++;
    }
    if (!apply) return response("PREVIEW");
    if (counts.stale || counts.missing || counts.invalid) return response("BLOCKED", 409);
    for (const { entry: e, index, state } of items) {
      if (state === "ALREADY_RECONCILED") continue;
      // A single atomic UPDATE rechecks the identity and current relevance/preference.
      // No recipient IDs leave D1 and no event/publication/immutable notification fields change.
      const changed = await env.DB.prepare(`UPDATE notifications SET status='FAILED',last_error=?
        WHERE id=? AND event_id=? AND status='PENDING' AND EXISTS (
          SELECT 1 FROM publication_events e JOIN publications p ON p.id=e.publication_id
          JOIN sources s ON s.id=p.source_id JOIN users u ON u.telegram_id=notifications.telegram_id
          WHERE e.id=notifications.event_id AND s.code=? AND p.external_id=?
          AND u.notify_direct IN (0,1) AND u.notify_potential IN (0,1)
          AND ((?='reclassified_irrelevant' AND p.relevance='IRRELEVANT')
            OR (?='reclassified_not_eligible' AND ((p.relevance='DIRECT' AND u.notify_direct=0)
              OR (p.relevance='POTENTIAL' AND u.notify_potential=0))))
          AND NOT EXISTS (SELECT 1 FROM ingestion_runs r WHERE r.source_id=p.source_id AND r.status IN ('OPEN','COMMITTING'))
        )`).bind(e.expected_reconciliation_reason, e.notification_id, e.event_id, e.source, e.external_id,
          e.expected_reconciliation_reason, e.expected_reconciliation_reason).run();
      if (changed.meta.changes !== 1) {
        counts.blocked++; counts.ready--; results.find(x => x.index === index)!.state = "STALE";
        return response(counts.writes ? "PARTIAL" : "CONFLICT", 409);
      }
      counts.applied++; counts.writes++; counts.ready--;
    }
    return response("APPLIED");
  } catch {
    // writes counts acknowledgements only: a transport failure may be ambiguous.
    // Never retry automatically or claim earlier updates were rolled back.
    return response("INDETERMINATE", 503);
  }
}
