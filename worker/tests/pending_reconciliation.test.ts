import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";
import worker from "../src/index.ts";
import { canonicalPlan, sha256 } from "../src/reclassification.ts";
import type { PendingReconciliationEnv } from "../src/pending_reconciliation.ts";

class LocalD1 {
  sqlite = new DatabaseSync(":memory:");
  beforeUpdate?: () => void;
  afterUpdate?: () => void;
  updates = 0;
  exec(sql: string) { this.sqlite.exec(sql); }
  close() { this.sqlite.close(); }
  prepare(sql: string) {
    const statement = this.sqlite.prepare(sql);
    const bound = (values: any[]) => ({
      first: <T>() => (statement.get(...values) as T | undefined) ?? null,
      all: <T>() => ({ results: statement.all(...values) as T[] }),
      run: () => {
        const update = sql.startsWith("UPDATE notifications SET status='FAILED'");
        if (update) { this.updates++; this.beforeUpdate?.(); }
        const result = statement.run(...values);
        if (update) this.afterUpdate?.();
        return { success: true, meta: { changes: Number(result.changes) } };
      },
    });
    return { ...bound([]), bind: (...values: any[]) => bound(values) };
  }
}
const token = "private-test-token";
async function fixture() {
  const db = new LocalD1(); db.exec("PRAGMA foreign_keys=ON");
  for (const file of ["0001_initial_schema.sql", "0002_ingestion.sql", "0003_ingestion_safety.sql", "0004_historical_baseline_gate.sql", "0005_notification_delivery_claim.sql", "0007_snapshot_ingestion_state.sql", "0008_source_deletions.sql"])
    db.exec(await readFile(new URL("../migrations/" + file, import.meta.url), "utf8"));
  db.exec(`INSERT INTO publications (id,source_id,external_id,external_url,original_title,relevance,discovered_at,updated_at,ruleset_version)
    VALUES (1,2,'70190','https://hoeringsportalen.dk/Hearing/Details/70190','private title','IRRELEVANT','t','t','1.3');
    INSERT INTO users (telegram_id,notify_direct,notify_potential,created_at,updated_at) VALUES (11,1,1,'t','t'),(22,0,1,'t','t');
    INSERT INTO publication_events (id,publication_id,event_key,event_type,detected_at,notify_eligible) VALUES
      (1743,1,'ingest:fixture:1743','RECLASSIFIED','2026-10-24T17:27:13.184Z',1),
      (1722,1,'ingest:fixture:1722','TITLE_CHANGED','2026-10-23T00:00:00Z',1);
    INSERT INTO notifications (id,telegram_id,event_id,status,attempts,created_at,sent_at,last_error) VALUES
      (1,11,1722,'SENT',1,'created','sent-time',NULL),
      (2,11,1743,'PENDING',0,'created',NULL,NULL),
      (3,22,1743,'PENDING',0,'created',NULL,NULL),
      (4,22,1722,'FAILED',1,'created',NULL,'unrelated_failure');`);
  const entries = [2, 3].map(notification_id => ({ notification_id, event_id: 1743, source: "hoeringsportalen", external_id: "70190",
    expected_status: "PENDING", expected_reconciliation_reason: "reclassified_irrelevant" }));
  const plan = { entries };
  const env: PendingReconciliationEnv = { DB: db as never, INGEST_API_TOKEN: token, PENDING_RECONCILIATION_WRITE_ENABLED: "true",
    PENDING_RECONCILIATION_PLAN_SHA256: await sha256(canonicalPlan(plan)), PENDING_RECONCILIATION_EXPECTED_COUNT: "2" };
  return { db, plan, env };
}
type Fixture = Awaited<ReturnType<typeof fixture>>;
async function pin(f: Fixture) {
  f.env.PENDING_RECONCILIATION_PLAN_SHA256 = await sha256(canonicalPlan(f.plan));
  f.env.PENDING_RECONCILIATION_EXPECTED_COUNT = String(f.plan.entries.length);
}
async function call(f: Fixture, action = "apply", options: { auth?: boolean; env?: object; plan?: unknown; confirmation?: string; method?: string } = {}) {
  const method = options.method ?? "POST";
  const response = await worker.fetch(new Request("https://local.invalid/internal/notifications/reconcile/" + action, {
    method, headers: { Authorization: options.auth === false ? "Bearer bad" : "Bearer " + token },
    ...(method === "POST" ? { body: JSON.stringify({ plan: options.plan ?? f.plan,
      ...(action === "apply" ? { confirmation: options.confirmation ?? "APPLY_PINNED_PENDING_RECONCILIATION" } : {}) }) } : {}),
  }), { ...f.env, ...options.env });
  return { status: response.status, body: await response.json() as any };
}
const rows = (f: Fixture) => f.db.prepare("SELECT * FROM notifications ORDER BY id").all<any>().results;
const snapshot = (f: Fixture) => canonicalPlan({ n: rows(f),
  p: f.db.prepare("SELECT * FROM publications").all().results, e: f.db.prepare("SELECT * FROM publication_events").all().results });

for (const action of ["preview", "apply"]) test(action + " requires bearer auth", async () => {
  const f = await fixture(); try { assert.equal((await call(f, action, { auth: false })).status, 401); assert.equal(f.db.updates, 0); } finally { f.db.close(); }
});
for (const [label, options, error] of [
  ["default write gate", { env: { PENDING_RECONCILIATION_WRITE_ENABLED: undefined } }, "pending_reconciliation_blocked"],
  ["exact true write gate", { env: { PENDING_RECONCILIATION_WRITE_ENABLED: "TRUE" } }, "pending_reconciliation_blocked"],
  ["delivery on", { env: { NOTIFICATION_DELIVERY_ENABLED: "true" } }, "pending_reconciliation_blocked"],
  ["fixed confirmation", { confirmation: "wrong" }, "invalid_confirmation"],
  ["plan pin", { env: { PENDING_RECONCILIATION_PLAN_SHA256: "0".repeat(64) } }, "plan_hash_mismatch"],
  ["count pin", { env: { PENDING_RECONCILIATION_EXPECTED_COUNT: "1" } }, "plan_count_mismatch"],
  ["noncanonical count", { env: { PENDING_RECONCILIATION_EXPECTED_COUNT: "02" } }, "plan_count_mismatch"],
  ["reclassification pins cannot authorize", { env: { PENDING_RECONCILIATION_PLAN_SHA256: undefined, RECLASSIFICATION_PLAN_SHA256: "a".repeat(64), RECLASSIFICATION_WRITE_ENABLED: "true" } }, "plan_hash_mismatch"],
] as const) test(label + " blocks writes", async () => {
  const f = await fixture(); try { const before = snapshot(f); const r = await call(f, "apply", options);
    assert.equal(r.status, error === "invalid_confirmation" ? 400 : 409); assert.equal(r.body.error, error); assert.equal(snapshot(f), before); assert.equal(f.db.updates, 0);
  } finally { f.db.close(); }
});
test("preview irrelevant is READY and read-only with both gates unconfigured", async () => {
  const f = await fixture(); try { const before = snapshot(f);
    const r = await call(f, "preview", { env: { PENDING_RECONCILIATION_WRITE_ENABLED: undefined, PENDING_RECONCILIATION_PLAN_SHA256: undefined, PENDING_RECONCILIATION_EXPECTED_COUNT: undefined } });
    assert.equal(r.status, 200); assert.equal(r.body.status, "PREVIEW"); assert.equal(r.body.ready, 2); assert.equal(r.body.irrelevant_count, 2);
    assert.deepEqual(r.body.entries.map((x: any) => x.state), ["READY", "READY"]);
    assert.equal(snapshot(f), before); assert.equal(f.db.updates, 0);
  } finally { f.db.close(); }
});
test("event 1743 x2 terminalizes only PENDING; event 1722 SENT and all immutable data preserved; replay idempotent", async () => {
  const f = await fixture(); try {
    const before = rows(f); const pubEvents = snapshot(f);
    const r = await call(f); assert.equal(r.status, 200); assert.equal(r.body.applied, 2); assert.equal(r.body.writes, 2);
    const after = rows(f);
    for (let i = 0; i < before.length; i++) {
      if (before[i].event_id === 1743) assert.deepEqual({ ...after[i] }, { ...before[i], status: "FAILED", last_error: "reclassified_irrelevant" });
      else assert.deepEqual(after[i], before[i]);
    }
    assert.equal(after[1].attempts, 0); assert.equal(after[1].sent_at, null);
    // No publication updates or invented events were needed for queue maintenance.
    const original = JSON.parse(pubEvents);
    assert.equal(canonicalPlan(f.db.prepare("SELECT * FROM publications").all().results), canonicalPlan(original.p));
    assert.equal(canonicalPlan(f.db.prepare("SELECT * FROM publication_events").all().results), canonicalPlan(original.e));
    const stable = snapshot(f); const repeat = await call(f);
    assert.equal(repeat.body.ready, 0); assert.equal(repeat.body.already_reconciled, 2); assert.equal(repeat.body.writes, 0); assert.equal(snapshot(f), stable);
    const preview = await call(f, "preview"); assert.equal(preview.body.already_reconciled, 2);
  } finally { f.db.close(); }
});
for (const relevance of ["DIRECT", "POTENTIAL"]) test(relevance + " preference denial READY/apply; enabling before apply blocks whole plan", async () => {
  const f = await fixture(); try {
    f.db.exec(`UPDATE publications SET relevance='${relevance}'; UPDATE users SET notify_direct=0,notify_potential=0;`);
    for (const e of f.plan.entries) e.expected_reconciliation_reason = "reclassified_not_eligible";
    await pin(f); assert.equal((await call(f, "preview")).body.ready, 2);
    const flag = relevance === "DIRECT" ? "notify_direct" : "notify_potential";
    f.db.exec(`UPDATE users SET ${flag}=1 WHERE telegram_id=22`);
    const stale = await call(f); assert.equal(stale.status, 409); assert.equal(stale.body.status, "BLOCKED"); assert.equal(stale.body.stale, 1);
    assert.equal(f.db.updates, 0); assert.equal(rows(f)[1].status, "PENDING");
    f.db.exec(`UPDATE users SET ${flag}=0 WHERE telegram_id=22`);
    const r = await call(f); assert.equal(r.status, 200); assert.equal(r.body.preference_count, 2);
    for (const n of rows(f).filter(n => n.event_id === 1743)) assert.equal(n.last_error, "reclassified_not_eligible");
  } finally { f.db.close(); }
});
for (const patch of [
  { notification_id: 999 }, { event_id: 1722 }, { source: "folketinget" }, { external_id: "999" },
  { expected_status: "SENT" }, { expected_reconciliation_reason: "arbitrary" }, { telegram_id: 123 },
  { notification_id: true }, { notification_id: Number.MAX_SAFE_INTEGER + 1 },
] as const) test("invalid or wrong identity " + JSON.stringify(patch) + " prevents any writes", async () => {
  const f = await fixture(); try {
    Object.assign(f.plan.entries[1], patch); await pin(f); const before = snapshot(f); const r = await call(f);
    assert.equal(r.status, 409); assert.equal(r.body.writes, 0); assert.equal(snapshot(f), before); assert.equal(f.db.updates, 0);
    const state = r.body.entries[1].state;
    assert.ok(["INVALID", "MISSING", "STALE"].includes(state));
  } finally { f.db.close(); }
});
for (const status of ["SENT", "SENDING", "FAILED"]) test(status + " is immutable and blocks a mixed plan", async () => {
  const f = await fixture(); try {
    f.db.exec(`UPDATE notifications SET status='${status}',last_error='other' WHERE id=3`);
    const before = snapshot(f); const r = await call(f); assert.equal(r.status, 409); assert.equal(r.body.stale, 1);
    assert.equal(snapshot(f), before); assert.equal(f.db.updates, 0);
  } finally { f.db.close(); }
});
test("eligible publication and wrong reason are STALE; other suppression alone is out of scope", async () => {
  const f = await fixture(); try {
    f.db.exec("UPDATE publications SET relevance='POTENTIAL',historical=1,source_complete=0,source_deleted=1; UPDATE publication_events SET notify_eligible=0,detected_at='2000-01-01';");
    const r = await call(f); assert.equal(r.status, 409); assert.equal(r.body.stale, 2); assert.equal(f.db.updates, 0);
  } finally { f.db.close(); }
});
test("a valid but currently incorrect reason cannot authorize reconciliation", async () => {
  const f = await fixture(); try {
    f.plan.entries[1].expected_reconciliation_reason = "reclassified_not_eligible"; await pin(f);
    const before = snapshot(f); const r = await call(f); assert.equal(r.status, 409); assert.equal(r.body.stale, 1);
    assert.equal(r.body.writes, 0); assert.equal(snapshot(f), before);
  } finally { f.db.close(); }
});
test("Folketinget identities use the same general operation", async () => {
  const f = await fixture(); try {
    f.db.exec("UPDATE publications SET source_id=1,external_id='12345'");
    for (const e of f.plan.entries) { e.source = "folketinget"; e.external_id = "12345"; }
    await pin(f); assert.equal((await call(f)).body.applied, 2);
  } finally { f.db.close(); }
});
test("empty, extra-field and excessive plan counts reject before writes", async () => {
  const f = await fixture(); try {
    for (const plan of [{ entries: [] }, { entries: f.plan.entries, other: "private" },
      { entries: Array.from({ length: 1001 }, () => f.plan.entries[0]) }]) {
      assert.equal((await call(f, "preview", { plan })).status, 400);
    }
    const before = snapshot(f); assert.equal(f.db.updates, 0); assert.equal(snapshot(f), before);
  } finally { f.db.close(); }
});
test("missing current user relation is STALE", async () => {
  const f = await fixture(); try {
    f.db.exec("PRAGMA foreign_keys=OFF; DELETE FROM users WHERE telegram_id=22; PRAGMA foreign_keys=ON;");
    assert.equal((await call(f)).body.stale, 1); assert.equal(f.db.updates, 0);
  } finally { f.db.close(); }
});
test("duplicate plan, wrong method and unknown route fail closed", async () => {
  const f = await fixture(); try {
    f.plan.entries[1] = { ...f.plan.entries[0] }; await pin(f); assert.equal((await call(f)).body.invalid, 1);
    assert.equal((await call(f, "preview", { method: "GET" })).status, 405);
    assert.equal((await call(f, "other")).status, 404); assert.equal(f.db.updates, 0);
  } finally { f.db.close(); }
});
for (const race of ["claim", "relevance", "preference", "identity", "active_ingestion"]) test("write predicate catches race: " + race, async () => {
  const f = await fixture(); try {
    if (race === "preference") {
      f.db.exec("UPDATE publications SET relevance='POTENTIAL'; UPDATE users SET notify_potential=0;");
      for (const e of f.plan.entries) e.expected_reconciliation_reason = "reclassified_not_eligible";
      await pin(f);
    }
    f.db.beforeUpdate = () => {
      f.db.beforeUpdate = undefined;
      if (race === "claim") f.db.exec("UPDATE notifications SET status='SENDING' WHERE id=2");
      if (race === "relevance") f.db.exec("UPDATE publications SET relevance='POTENTIAL'");
      if (race === "preference") f.db.exec("UPDATE users SET notify_potential=1");
      if (race === "identity") f.db.exec("UPDATE publications SET external_id='999'");
      if (race === "active_ingestion") f.db.exec("INSERT INTO ingestion_runs (run_id,source_id,mode,expected_batches,collection_complete,status,proposed_checkpoints_json,base_checkpoints_json,start_request_hash,created_at,updated_at) VALUES ('r',2,'incremental',1,1,'OPEN','{}','{}','h','t','t')");
    };
    const r = await call(f); assert.equal(r.status, 409); assert.equal(r.body.writes, 0); assert.equal(r.body.blocked, 1);
    assert.equal(f.db.updates, 1); assert.equal(rows(f).find(n => n.id === 3).status, "PENDING");
  } finally { f.db.close(); }
});
test("late conflict reports partial acknowledged writes and stops", async () => {
  const f = await fixture(); try {
    f.db.beforeUpdate = () => { if (f.db.updates === 2) f.db.exec("UPDATE notifications SET status='SENDING' WHERE id=3"); };
    const r = await call(f); assert.equal(r.status, 409); assert.equal(r.body.status, "PARTIAL"); assert.equal(r.body.writes, 1);
    assert.equal(rows(f)[1].status, "FAILED"); assert.equal(rows(f)[2].status, "SENDING");
  } finally { f.db.close(); }
});
test("database errors are safe, ambiguous, and never retried", async () => {
  const f = await fixture(); try {
    f.db.afterUpdate = () => { throw new Error("PRIVATE token recipient title SQL"); };
    const r = await call(f); assert.equal(r.status, 503); assert.equal(r.body.status, "INDETERMINATE");
    assert.equal(f.db.updates, 1); assert.doesNotMatch(JSON.stringify(r.body), /PRIVATE|token|recipient|title|SQL|telegram_id/);
  } finally { f.db.close(); }
});
