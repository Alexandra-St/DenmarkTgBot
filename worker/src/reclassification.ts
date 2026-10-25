import { isIngestionRequestAuthorized } from "./ingestion.ts";

export interface ReclassificationEnv {
  DB: D1Database; INGEST_API_TOKEN?: string;
  RECLASSIFICATION_WRITE_ENABLED?: string;
  RECLASSIFICATION_PLAN_SHA256?: string;
  RECLASSIFICATION_EXPECTED_COUNT?: string;
  NOTIFICATION_DELIVERY_ENABLED?: string;
}
type Obj = Record<string, unknown>;
type State = "READY" | "ALREADY_APPLIED" | "STALE" | "MISSING" | "INVALID";
interface Entry {
  source: string; external_id: string; expected_relevance: string;
  expected_ruleset_version: string | null; expected_updated_at: string;
  expected_normalized_snapshot_sha256: string; new_relevance: string;
  new_relevance_reason: string; new_ruleset_version: string;
}
interface Row {
  id: number; source_id: number; relevance: string; relevance_reason: string | null;
  ruleset_version: string | null; updated_at: string; normalized_snapshot_json: string;
  source_complete: number; source_deleted: number;
}
const keys = ["source", "external_id", "expected_relevance", "expected_ruleset_version", "expected_updated_at",
  "expected_normalized_snapshot_sha256", "new_relevance", "new_relevance_reason", "new_ruleset_version"];
const object = (v: unknown): v is Obj => !!v && typeof v === "object" && !Array.isArray(v);
const relevance = (v: unknown) => ["DIRECT", "POTENTIAL", "IRRELEVANT"].includes(v as string);
const hash = (v: unknown): v is string => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
export function canonicalPlan(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonicalPlan).join(",") + "]";
  if (object(value)) return "{" + Object.keys(value).sort().map(k => JSON.stringify(k) + ":" + canonicalPlan(value[k])).join(",") + "}";
  return JSON.stringify(value) ?? "null";
}
export async function sha256(value: string): Promise<string> {
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value))))
    .map(x => x.toString(16).padStart(2, "0")).join("");
}
function valid(v: unknown): v is Entry {
  return object(v) && Object.keys(v).length === keys.length && keys.every(k => Object.hasOwn(v, k))
    && ["folketinget", "hoeringsportalen"].includes(v.source as string)
    && typeof v.external_id === "string" && /^[1-9][0-9]{0,18}$/.test(v.external_id)
    && relevance(v.expected_relevance) && relevance(v.new_relevance)
    && (v.expected_ruleset_version === null || (typeof v.expected_ruleset_version === "string" && /^\d{1,8}\.\d{1,8}$/.test(v.expected_ruleset_version)))
    && typeof v.expected_updated_at === "string" && v.expected_updated_at.length <= 40 && Number.isFinite(Date.parse(v.expected_updated_at))
    && hash(v.expected_normalized_snapshot_sha256)
    && typeof v.new_relevance_reason === "string" && v.new_relevance_reason.length <= 2000
    && typeof v.new_ruleset_version === "string" && /^\d{1,8}\.\d{1,8}$/.test(v.new_ruleset_version);
}
const json = (v: unknown, status = 200) => new Response(JSON.stringify(v), {
  status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
});
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
async function inspect(db: D1Database, e: Entry, eventKey: string): Promise<{ state: State; row?: Row }> {
  const row = await db.prepare("SELECT p.* FROM publications p JOIN sources s ON s.id=p.source_id WHERE s.code=? AND p.external_id=?")
    .bind(e.source, e.external_id).first<Row>();
  if (!row) return { state: "MISSING" };
  if (row.source_complete !== 1 || row.source_deleted !== 0) return { state: "STALE" };
  let snapshot: unknown;
  try { snapshot = JSON.parse(row.normalized_snapshot_json); } catch { return { state: "INVALID" }; }
  if (!object(snapshot) || snapshot.relevance !== row.relevance) return { state: "INVALID" };
  const digest = await sha256(row.normalized_snapshot_json);
  const audit = await db.prepare("SELECT change_snapshot_json, detected_at, event_type, notify_eligible FROM publication_events WHERE publication_id=? AND event_key=?")
    .bind(row.id, eventKey).first<{ change_snapshot_json: string; detected_at: string; event_type: string; notify_eligible: number }>();
  if (audit) {
    let detail: Obj;
    try { detail = JSON.parse(audit.change_snapshot_json); } catch { return { state: "INVALID" }; }
    return { state: audit.event_type === "RECLASSIFIED" && audit.notify_eligible === 0
      && detail.reason === "controlled_reclassification" && detail.snapshot_sha256 === digest
      && row.relevance === e.new_relevance && row.relevance_reason === e.new_relevance_reason
      && row.ruleset_version === e.new_ruleset_version && row.updated_at === audit.detected_at
      ? "ALREADY_APPLIED" : "STALE", row };
  }
  if (row.relevance === e.new_relevance && row.relevance_reason === e.new_relevance_reason
      && row.ruleset_version === e.new_ruleset_version) return { state: "INVALID" };
  return { state: row.relevance === e.expected_relevance && row.ruleset_version === e.expected_ruleset_version
    && row.updated_at === e.expected_updated_at && digest === e.expected_normalized_snapshot_sha256 ? "READY" : "STALE", row };
}

/** Per-publication D1 batch transaction; no global transaction across the plan. */
export async function handleReclassificationRequest(request: Request, env: ReclassificationEnv): Promise<Response> {
  if (!await isIngestionRequestAuthorized(request, env)) return json({ error: "unauthorized" }, 401);
  if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
  const apply = new URL(request.url).pathname === "/internal/reclassification/apply";
  if (!apply && new URL(request.url).pathname !== "/internal/reclassification/preview") return json({ error: "not_found" }, 404);
  if (apply && (env.RECLASSIFICATION_WRITE_ENABLED !== "true" || env.NOTIFICATION_DELIVERY_ENABLED === "true"))
    return json({ error: "reclassification_blocked" }, 409);
  let body: unknown;
  try { body = await readBody(request); } catch { return json({ error: "invalid_plan" }, 400); }
  if (!object(body) || Object.keys(body).some(k => !["plan", ...(apply ? ["confirmation"] : [])].includes(k))
      || !object(body.plan) || Object.keys(body.plan).length !== 1 || !Array.isArray(body.plan.entries)
      || body.plan.entries.length < 1 || body.plan.entries.length > 1000) return json({ error: "invalid_plan" }, 400);
  if (apply && body.confirmation !== "APPLY_PINNED_RECLASSIFICATION_PLAN") return json({ error: "invalid_confirmation" }, 400);
  const entries = body.plan.entries;
  const digest = await sha256(canonicalPlan(body.plan));
  if (apply && (!hash(env.RECLASSIFICATION_PLAN_SHA256) || env.RECLASSIFICATION_PLAN_SHA256 !== digest))
    return json({ error: "plan_hash_mismatch" }, 409);
  if (apply && (!/^[1-9][0-9]{0,3}$/.test(env.RECLASSIFICATION_EXPECTED_COUNT ?? "")
      || Number(env.RECLASSIFICATION_EXPECTED_COUNT) !== entries.length)) return json({ error: "plan_count_mismatch" }, 409);
  const counts = { ready: 0, applied: 0, already_applied: 0, stale: 0, missing: 0, invalid: 0, blocked: 0 };
  const conflicts: Array<{ source: string; external_id: string; state: State }> = [];
  const seen = new Set<string>();
  const items: Array<{ e: Entry; key: string; state: State }> = [];
  try {
    // Inspect the whole plan before any writes. A malformed/stale plan writes nothing.
    for (const raw of entries) {
      if (!valid(raw)) { counts.invalid++; continue; }
      const id = raw.source + ":" + raw.external_id;
      if (seen.has(id)) { counts.invalid++; continue; } seen.add(id);
      const key = "reclassification:" + digest + ":" + await sha256(id);
      const { state } = await inspect(env.DB, raw, key);
      items.push({ e: raw, key, state });
      if (state === "READY") counts.ready++;
      else if (state === "ALREADY_APPLIED") counts.already_applied++;
      else { counts[state.toLowerCase() as "stale" | "missing" | "invalid"]++; conflicts.push({ source: raw.source, external_id: raw.external_id, state }); }
    }
    if (!apply) return json({ status: "PREVIEW", plan_sha256: digest, expected_count: entries.length, ...counts, conflicts });
    if (counts.stale || counts.missing || counts.invalid) return json({ status: "CONFLICT", ...counts, conflicts }, 409);
    for (let i = 0; i < items.length; i++) {
      const { e, key, state } = items[i]; if (state === "ALREADY_APPLIED") continue;
      const checked = await inspect(env.DB, e, key);
      if (checked.state === "ALREADY_APPLIED") { counts.already_applied++; counts.ready--; continue; }
      if (checked.state !== "READY" || !checked.row) {
        counts[checked.state.toLowerCase() as "stale" | "missing" | "invalid"]++; counts.ready--;
        conflicts.push({ source: e.source, external_id: e.external_id, state: checked.state }); break;
      }
      const row = checked.row; const now = new Date().toISOString();
      // Current ingestion stores only relevance in the normalized classification state.
      const updated = canonicalPlan({ ...JSON.parse(row.normalized_snapshot_json), relevance: e.new_relevance });
      const audit = canonicalPlan({ previous_relevance: e.expected_relevance, new_relevance: e.new_relevance,
        previous_ruleset_version: e.expected_ruleset_version, new_ruleset_version: e.new_ruleset_version,
        reason: "controlled_reclassification", snapshot_sha256: await sha256(updated) });
      const results = await env.DB.batch([
        env.DB.prepare(`UPDATE publications SET relevance=?, relevance_reason=?, ruleset_version=?, normalized_snapshot_json=?, updated_at=?
          WHERE id=? AND source_id=? AND external_id=? AND relevance=? AND ruleset_version IS ? AND updated_at=?
          AND normalized_snapshot_json=? AND relevance_reason IS ? AND source_complete=1 AND source_deleted=0
          AND NOT EXISTS (SELECT 1 FROM publication_events WHERE event_key=?)
          AND NOT EXISTS (SELECT 1 FROM notifications n JOIN publication_events e ON e.id=n.event_id WHERE e.publication_id=publications.id AND n.status='SENDING')
          AND NOT EXISTS (SELECT 1 FROM ingestion_runs WHERE source_id=publications.source_id AND status IN ('OPEN','COMMITTING'))`)
          .bind(e.new_relevance, e.new_relevance_reason, e.new_ruleset_version, updated, now, row.id, row.source_id, e.external_id,
            e.expected_relevance, e.expected_ruleset_version, e.expected_updated_at, row.normalized_snapshot_json, row.relevance_reason, key),
        env.DB.prepare(`INSERT INTO publication_events (publication_id,event_key,event_type,detected_at,notify_eligible,change_snapshot_json)
          SELECT ?,?,'RECLASSIFIED',?,0,? WHERE changes()=1`).bind(row.id, key, now, audit),
        env.DB.prepare(`UPDATE notifications SET status='FAILED', last_error=? WHERE changes()=1 AND status='PENDING'
          AND event_id IN (SELECT id FROM publication_events WHERE publication_id=?)
          AND (?='IRRELEVANT' OR NOT EXISTS (SELECT 1 FROM users u WHERE u.telegram_id=notifications.telegram_id
            AND ((?='DIRECT' AND u.notify_direct=1) OR (?='POTENTIAL' AND u.notify_potential=1))))`)
          .bind(e.new_relevance === "IRRELEVANT" ? "reclassified_irrelevant" : "reclassified_not_eligible", row.id,
            e.new_relevance, e.new_relevance, e.new_relevance),
      ]);
      if (results[0].meta.changes !== 1) {
        const after = await inspect(env.DB, e, key);
        if (after.state === "ALREADY_APPLIED") { counts.already_applied++; counts.ready--; continue; }
        counts.blocked++; counts.ready--; conflicts.push({ source: e.source, external_id: e.external_id, state: "STALE" }); break;
      }
      if (results[1].meta.changes !== 1) throw new Error("maintenance_event_not_acknowledged");
      counts.applied++; counts.ready--;
    }
    const success = counts.applied + counts.already_applied === entries.length;
    return json({ status: success ? "APPLIED" : "PARTIAL", ...counts, conflicts }, success ? 200 : 409);
  } catch {
    // A transport failure can be ambiguous. Never claim rollback of earlier batches.
    return json({ status: "INDETERMINATE", error: "reclassification_storage_failed", ...counts }, 503);
  }
}
