import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { DatabaseSync, type StatementSync } from "node:sqlite";
import { test } from "node:test";
import worker from "../src/index.ts";
import { canonicalPlan, sha256, type ReclassificationEnv } from "../src/reclassification.ts";
import { handleIngestionRequest } from "../src/ingestion.ts";
const batchExecute = Symbol("batchExecute");

class LocalD1Fixture {
  private readonly sqlite: DatabaseSync;
  constructor(path = ":memory:") {
    this.sqlite = new DatabaseSync(path);
    this.sqlite.exec("PRAGMA foreign_keys = ON");
  }
  exec(sql: string): void { this.sqlite.exec(sql); }
  prepare(sql: string) {
    const statement = this.sqlite.prepare(sql);
    return {
      bind: (...values: unknown[]) => this.bound(statement, values),
      all: <T>() => ({ results: statement.all() as T[] }),
      first: <T>() => (statement.get() as T | undefined) ?? null,
      run: () => {
        const result = statement.run();
        return { success: true, meta: { changes: result.changes, last_row_id: Number(result.lastInsertRowid) } };
      },
    };
  }
  private bound(statement: StatementSync, values: unknown[]) {
    return {
      all: <T>() => ({ results: statement.all(...values) as T[] }),
      first: <T>() => (statement.get(...values) as T | undefined) ?? null,
      run: () => {
        const result = statement.run(...values);
        return { success: true, meta: { changes: result.changes, last_row_id: Number(result.lastInsertRowid) } };
      },
      [batchExecute]: () => {
        const result = statement.run(...values);
        return { success: true, meta: { changes: result.changes, last_row_id: Number(result.lastInsertRowid) } };
      },
    };
  }
  async batch(statements: Array<Record<PropertyKey, unknown>>) {
    this.sqlite.exec("BEGIN IMMEDIATE");
    try {
      const results = statements.map((statement) => {
        const execute = statement[batchExecute];
        if (typeof execute !== "function") throw new Error("invalid local D1 statement");
        return (execute as () => unknown)();
      });
      this.sqlite.exec("COMMIT");
      return results;
    } catch (error) {
      this.sqlite.exec("ROLLBACK");
      throw error;
    }
  }
  close(): void { this.sqlite.close(); }
}

const token = "local-only-token";
const time = "2026-10-25T10:00:00.000Z";
async function fixture() {
  const db = new LocalD1Fixture();
  for (const file of ["0001_initial_schema.sql", "0002_ingestion.sql", "0003_ingestion_safety.sql", "0004_historical_baseline_gate.sql", "0005_notification_delivery_claim.sql", "0007_snapshot_ingestion_state.sql", "0008_source_deletions.sql"])
    db.exec(await readFile(new URL("../migrations/" + file, import.meta.url), "utf8"));
  const snapshot = { title: "Official title", content_text: "Original content", status: null, publication_type: null,
    relevance: "POTENTIAL", documents: [], stages: [], votes: [] };
  db.prepare(`INSERT INTO publications (id,source_id,external_id,external_url,original_title,relevance,relevance_reason,
    discovered_at,updated_at,normalized_snapshot_json,ruleset_version) VALUES (1,2,'70190',
    'https://hoeringsportalen.dk/Hearing/Details/70190','Official title','POTENTIAL','old reason',?,?,?,'1.2')`)
    .bind(time, time, canonicalPlan(snapshot)).run();
  db.exec(`INSERT INTO users (telegram_id,notify_direct,notify_potential,created_at,updated_at) VALUES
    (11,1,0,'t','t'), (22,0,1,'t','t'), (33,1,1,'t','t');
    INSERT INTO publication_events (id,publication_id,event_key,event_type,detected_at) VALUES
    (1743,1,'ingest:old:1743','TITLE_CHANGED','t'), (1722,1,'ingest:old:1722','TITLE_CHANGED','t');
    INSERT INTO notifications (telegram_id,event_id,status,created_at,attempts,sent_at,last_error) VALUES
    (11,1743,'PENDING','t',0,NULL,NULL), (22,1743,'PENDING','t',0,NULL,NULL),
    (11,1722,'SENT','t',1,'sent-time',NULL), (22,1722,'FAILED','t',1,NULL,'old_failure');`);
  const entry = { source: "hoeringsportalen", external_id: "70190", expected_relevance: "POTENTIAL",
    expected_ruleset_version: "1.2", expected_updated_at: time,
    expected_normalized_snapshot_sha256: await sha256(canonicalPlan(snapshot)),
    new_relevance: "IRRELEVANT", new_relevance_reason: "territorial scope", new_ruleset_version: "1.3" };
  const plan = { entries: [entry] };
  const env: ReclassificationEnv = { DB: db as never, INGEST_API_TOKEN: token, RECLASSIFICATION_WRITE_ENABLED: "true",
    RECLASSIFICATION_PLAN_SHA256: await sha256(canonicalPlan(plan)), RECLASSIFICATION_EXPECTED_COUNT: "1" };
  return { db, snapshot, entry, plan, env };
}
async function call(f: Awaited<ReturnType<typeof fixture>>, action = "apply", options: { auth?: boolean; confirmation?: string; env?: object; plan?: unknown } = {}) {
  const response = await worker.fetch(new Request("https://local.invalid/internal/reclassification/" + action, {
    method: "POST", headers: { Authorization: options.auth === false ? "Bearer bad" : "Bearer " + token },
    body: JSON.stringify({ plan: options.plan ?? f.plan, ...(action === "apply" ? { confirmation: options.confirmation ?? "APPLY_PINNED_RECLASSIFICATION_PLAN" } : {}) }),
  }), { ...f.env, ...options.env });
  return { status: response.status, body: await response.json() as any };
}
async function repin(f: Awaited<ReturnType<typeof fixture>>) {
  f.env.RECLASSIFICATION_PLAN_SHA256 = await sha256(canonicalPlan(f.plan));
  f.env.RECLASSIFICATION_EXPECTED_COUNT = String(f.plan.entries.length);
}
const pub = (db: LocalD1Fixture) => db.prepare("SELECT * FROM publications WHERE id=1").first<any>();
const notifications = (db: LocalD1Fixture) => db.prepare("SELECT * FROM notifications ORDER BY id").all<any>().results;

for (const action of ["preview", "apply"]) test(action + " requires ingestion authorization", async () => {
  const f = await fixture(); try { assert.equal((await call(f, action, { auth: false })).status, 401); } finally { f.db.close(); }
});
for (const [name, options, error] of [
  ["write gate default off", { env: { RECLASSIFICATION_WRITE_ENABLED: undefined } }, "reclassification_blocked"],
  ["write gate exact true", { env: { RECLASSIFICATION_WRITE_ENABLED: "TRUE" } }, "reclassification_blocked"],
  ["delivery enabled", { env: { NOTIFICATION_DELIVERY_ENABLED: "true" } }, "reclassification_blocked"],
  ["confirmation", { confirmation: "wrong" }, "invalid_confirmation"],
  ["hash", { env: { RECLASSIFICATION_PLAN_SHA256: "a".repeat(64) } }, "plan_hash_mismatch"],
  ["invalid pin", { env: { RECLASSIFICATION_PLAN_SHA256: "private-invalid" } }, "plan_hash_mismatch"],
  ["count", { env: { RECLASSIFICATION_EXPECTED_COUNT: "2" } }, "plan_count_mismatch"],
  ["invalid count", { env: { RECLASSIFICATION_EXPECTED_COUNT: "01" } }, "plan_count_mismatch"],
] as const) test(name + " fails closed without writes", async () => {
  const f = await fixture(); try {
    const before = canonicalPlan(pub(f.db)); const result = await call(f, "apply", options);
    assert.equal(result.body.error, error); assert.ok(result.status >= 400); assert.equal(canonicalPlan(pub(f.db)), before);
  } finally { f.db.close(); }
});
test("READY preview has zero writes even with write gate disabled", async () => {
  const f = await fixture(); try {
    const before = canonicalPlan({ p: pub(f.db), n: notifications(f.db) });
    const r = await call(f, "preview", { env: { RECLASSIFICATION_WRITE_ENABLED: undefined } });
    assert.equal(r.body.ready, 1); assert.equal(r.body.applied, 0);
    assert.equal(canonicalPlan({ p: pub(f.db), n: notifications(f.db) }), before);
    assert.equal(f.db.prepare("SELECT COUNT(*) n FROM publication_events").first<any>().n, 2);
  } finally { f.db.close(); }
});
for (const [name, patch] of [
  ["relevance", { expected_relevance: "DIRECT" }], ["ruleset", { expected_ruleset_version: "1.1" }],
  ["timestamp", { expected_updated_at: "2026-10-24T10:00:00.000Z" }],
  ["snapshot", { expected_normalized_snapshot_sha256: "0".repeat(64) }],
] as const) test("stale " + name + " blocks preview and apply", async () => {
  const f = await fixture(); try {
    Object.assign(f.entry, patch); await repin(f);
    assert.equal((await call(f, "preview")).body.stale, 1);
    const r = await call(f); assert.equal(r.status, 409); assert.equal(r.body.applied, 0);
  } finally { f.db.close(); }
});
test("missing publication", async () => {
  const f = await fixture(); try {
    f.entry.external_id = "999"; await repin(f);
    assert.equal((await call(f, "preview")).body.missing, 1); assert.equal((await call(f)).status, 409);
  } finally { f.db.close(); }
});
test("malformed, duplicate, incomplete and deleted entries fail closed", async () => {
  const f = await fixture(); try {
    assert.equal((await call(f, "preview", { plan: { entries: [{ ...f.entry, sql: "secret" }] } })).body.invalid, 1);
    f.plan.entries.push({ ...f.entry }); await repin(f); assert.equal((await call(f)).status, 409);
    f.plan.entries.pop(); await repin(f);
    f.db.exec("UPDATE publications SET source_complete=0"); assert.equal((await call(f)).status, 409);
    f.db.exec("UPDATE publications SET source_complete=1,source_deleted=1"); assert.equal((await call(f)).status, 409);
  } finally { f.db.close(); }
});
test("event 1743: POTENTIAL to IRRELEVANT atomically suppresses both pending rows; terminal audit immutable; replay idempotent", async () => {
  const f = await fixture(); try {
    const before = pub(f.db); const terminal = notifications(f.db).filter(n => n.status !== "PENDING");
    const r = await call(f); assert.equal(r.status, 200); assert.equal(r.body.applied, 1);
    const after = pub(f.db); assert.equal(after.relevance, "IRRELEVANT"); assert.equal(after.ruleset_version, "1.3");
    assert.equal(after.relevance_reason, "territorial scope");
    assert.deepEqual(JSON.parse(after.normalized_snapshot_json), { ...f.snapshot, relevance: "IRRELEVANT" });
    for (const k of Object.keys(before).filter(k => !["relevance", "relevance_reason", "ruleset_version", "normalized_snapshot_json", "updated_at"].includes(k))) assert.deepEqual(after[k], before[k], k);
    const pending = notifications(f.db).filter(n => n.event_id === 1743);
    assert.equal(pending.length, 2); for (const n of pending) {
      assert.equal(n.status, "FAILED"); assert.equal(n.last_error, "reclassified_irrelevant"); assert.equal(n.attempts, 0); assert.equal(n.sent_at, null);
    }
    assert.deepEqual(notifications(f.db).filter(n => n.event_id === 1722), terminal);
    const audit = f.db.prepare("SELECT * FROM publication_events WHERE event_type='RECLASSIFIED'").all<any>().results;
    assert.equal(audit.length, 1); assert.equal(audit[0].notify_eligible, 0); assert.match(audit[0].event_key, /^reclassification:/);
    const snapshot = JSON.parse(audit[0].change_snapshot_json); assert.equal(snapshot.previous_relevance, "POTENTIAL"); assert.equal(snapshot.new_relevance, "IRRELEVANT");
    assert.equal(snapshot.reason, "controlled_reclassification");
    const saved = canonicalPlan({ p: after, n: notifications(f.db), audit });
    const repeat = await call(f); assert.equal(repeat.body.applied, 0); assert.equal(repeat.body.already_applied, 1);
    assert.equal(canonicalPlan({ p: pub(f.db), n: notifications(f.db), audit: f.db.prepare("SELECT * FROM publication_events WHERE event_type='RECLASSIFIED'").all().results }), saved);
    assert.equal((await call(f, "preview")).body.already_applied, 1);
  } finally { f.db.close(); }
});
for (const [old, next] of [["DIRECT", "POTENTIAL"], ["POTENTIAL", "DIRECT"]]) test(old + " to " + next + " respects current user preferences", async () => {
  const f = await fixture(); try {
    f.entry.expected_relevance = old; f.entry.new_relevance = next;
    const snap = { ...f.snapshot, relevance: old };
    f.db.prepare("UPDATE publications SET relevance=?,normalized_snapshot_json=?").bind(old, canonicalPlan(snap)).run();
    f.entry.expected_normalized_snapshot_sha256 = await sha256(canonicalPlan(snap)); await repin(f);
    const r = await call(f); assert.equal(r.status, 200);
    for (const n of notifications(f.db).filter(n => n.event_id === 1743)) {
      const eligible = next === "DIRECT" ? n.telegram_id === 11 : n.telegram_id === 22;
      assert.equal(n.status, eligible ? "PENDING" : "FAILED");
      assert.equal(n.last_error, eligible ? null : "reclassified_not_eligible");
    }
  } finally { f.db.close(); }
});
test("SENDING blocks publication mutation and is never overwritten", async () => {
  const f = await fixture(); try {
    f.db.exec("UPDATE notifications SET status='SENDING' WHERE id=1"); const before = canonicalPlan(pub(f.db));
    const r = await call(f); assert.equal(r.status, 409); assert.equal(r.body.blocked, 1);
    assert.equal(canonicalPlan(pub(f.db)), before); assert.equal(notifications(f.db)[0].status, "SENDING");
  } finally { f.db.close(); }
});
test("CAS conflict between inspection and batch writes nothing", async () => {
  const f = await fixture(); try {
    const batch = f.db.batch.bind(f.db); f.db.batch = async statements => {
      f.db.exec("UPDATE publications SET updated_at='concurrent-change'"); return batch(statements);
    };
    const r = await call(f); assert.equal(r.status, 409); assert.equal(r.body.applied, 0);
    assert.equal(pub(f.db).relevance, "POTENTIAL"); assert.equal(notifications(f.db)[0].status, "PENDING");
    assert.equal(f.db.prepare("SELECT COUNT(*) n FROM publication_events").first<any>().n, 2);
  } finally { f.db.close(); }
});
test("event insertion error rolls back publication and notification updates", async () => {
  const f = await fixture(); try {
    f.db.exec("CREATE TRIGGER reject_maintenance BEFORE INSERT ON publication_events WHEN NEW.event_type='RECLASSIFIED' BEGIN SELECT RAISE(ABORT,'private SQL secret'); END");
    const before = canonicalPlan(pub(f.db)); const r = await call(f); assert.equal(r.status, 503);
    assert.equal(canonicalPlan(pub(f.db)), before); assert.equal(notifications(f.db)[0].status, "PENDING");
    assert.ok(!JSON.stringify(r.body).includes("private SQL secret"));
  } finally { f.db.close(); }
});
test("active source ingestion blocks apply", async () => {
  const f = await fixture(); try {
    f.db.exec(`INSERT INTO ingestion_runs (run_id,source_id,mode,expected_batches,collection_complete,status,proposed_checkpoints_json,
      base_checkpoints_json,start_request_hash,created_at,updated_at) VALUES ('active',2,'historical',1,1,'OPEN','{}','{}','h','t','t')`);
    assert.equal((await call(f)).body.blocked, 1);
  } finally { f.db.close(); }
});
test("snapshot mutation after applied operation is STALE, not idempotent success", async () => {
  const f = await fixture(); try {
    assert.equal((await call(f)).status, 200);
    f.db.exec("UPDATE publications SET normalized_snapshot_json=json_set(normalized_snapshot_json,'$.title','new source data')");
    const r = await call(f); assert.equal(r.status, 409); assert.equal(r.body.stale, 1);
  } finally { f.db.close(); }
});
test("equivalent next ordinary ingestion creates no duplicate RECLASSIFIED", async () => {
  const f = await fixture(); try {
    assert.equal((await call(f)).status, 200);
    const run = "00000000-0000-4000-8000-000000000099";
    f.db.exec(`INSERT INTO ingestion_runs (run_id,source_id,mode,expected_batches,collection_complete,status,proposed_checkpoints_json,
      base_checkpoints_json,start_request_hash,created_at,updated_at) VALUES ('baseline',2,'historical',1,1,'COMMITTED','{}','{}','h','t','t');
      INSERT INTO source_ingestion_baselines VALUES (2,'baseline','t')`);
    const ingest = async (body: unknown) => {
      const r = await handleIngestionRequest(new Request("https://local.invalid/internal/ingest", { method: "POST",
        headers: { Authorization: "Bearer " + token }, body: JSON.stringify(body) }), f.env);
      assert.ok(r.status < 300, await r.clone().text()); return r.json();
    };
    await ingest({ action: "start", run_id: run, source: "hoeringsportalen", mode: "incremental", collection_complete: true,
      expected_batches: 1, proposed_checkpoints: { immigration_ministry: null, immigration_category: null } });
    await ingest({ action: "batch", run_id: run, batch_index: 0, records: [{ source: "hoeringsportalen", external_id: "70190",
      title: f.snapshot.title, url: "https://hoeringsportalen.dk/Hearing/Details/70190", relevance: "IRRELEVANT",
      relevance_reason: "territorial scope", ruleset_version: "1.3", status: null, publication_type: null, published_at: null,
      source_updated_at: null, source_updated_at_raw: null, historical: false, notification_blocked: false, complete: true,
      diagnostics: [], source_metadata: {}, normalized_snapshot: { ...f.snapshot, relevance: "IRRELEVANT" } }] });
    await ingest({ action: "commit", run_id: run });
    assert.equal(f.db.prepare("SELECT COUNT(*) n FROM publication_events WHERE event_type='RECLASSIFIED'").first<any>().n, 1);
  } finally { f.db.close(); }
});
test("responses contain no notification identities, source contents, reasons, token or SQL", async () => {
  const f = await fixture(); try {
    const response = JSON.stringify((await call(f)).body);
    for (const privateValue of ["telegram_id", "Official title", "Original content", "territorial scope", token, "SELECT", "sent-time"]) assert.ok(!response.includes(privateValue));
  } finally { f.db.close(); }
});

test("partial plan stops after a per-publication conflict and resumes exactly", async () => {
  const f = await fixture(); try {
    const original = pub(f.db);
    for (const id of [2, 3]) {
      f.db.prepare(`INSERT INTO publications (id,source_id,external_id,external_url,original_title,relevance,relevance_reason,
        discovered_at,updated_at,normalized_snapshot_json,ruleset_version) VALUES (?,2,?,'https://hoeringsportalen.dk/Hearing/Details/70190',
        'Official title','POTENTIAL','old reason',?,?,?,'1.2')`).bind(id, String(70189 + id), time, time, original.normalized_snapshot_json).run();
      f.plan.entries.push({ ...f.entry, external_id: String(70189 + id) });
    }
    await repin(f);
    const batch = f.db.batch.bind(f.db); let batches = 0;
    f.db.batch = async statements => { if (++batches === 2) f.db.exec("UPDATE notifications SET status='SENDING' WHERE id=1");
      // The second publication has its own in-flight notification.
      if (batches === 2) f.db.exec(`INSERT INTO publication_events (id,publication_id,event_key,event_type,detected_at) VALUES (1800,2,'other','TITLE_CHANGED','t');
        INSERT INTO notifications (telegram_id,event_id,status,created_at) VALUES (33,1800,'SENDING','t')`);
      return batch(statements);
    };
    const result = await call(f); assert.equal(result.status, 409); assert.equal(result.body.status, "PARTIAL");
    assert.equal(result.body.applied, 1); assert.equal(result.body.blocked, 1); assert.equal(result.body.ready, 1); assert.equal(batches, 2);
    assert.equal(f.db.prepare("SELECT relevance FROM publications WHERE id=3").first<any>().relevance, "POTENTIAL");
    f.db.batch = batch;
    f.db.exec("UPDATE notifications SET status='FAILED' WHERE status='SENDING'");
    const repeat = await call(f); assert.equal(repeat.status, 200); assert.equal(repeat.body.applied, 2); assert.equal(repeat.body.already_applied, 1);
    assert.equal(f.db.prepare("SELECT COUNT(*) n FROM publication_events WHERE event_type='RECLASSIFIED'").first<any>().n, 3);
  } finally { f.db.close(); }
});
test("concurrent same-plan callers create exactly one event and one suppression", async () => {
  const f = await fixture(); try {
    const results = await Promise.all([call(f), call(f)]);
    assert.ok(results.every(r => r.status === 200));
    assert.equal(results.reduce((n, r) => n + r.body.applied, 0), 1);
    assert.equal(f.db.prepare("SELECT COUNT(*) n FROM publication_events WHERE event_type='RECLASSIFIED'").first<any>().n, 1);
    assert.equal(notifications(f.db).filter(n => n.last_error === "reclassified_irrelevant").length, 2);
  } finally { f.db.close(); }
});
test("pin covers the entire plan and normalized snapshot hash covers exact stored bytes", async () => {
  const f = await fixture(); try {
    assert.equal(canonicalPlan({ b: 1, a: [2, 1] }), '{"a":[2,1],"b":1}');
    assert.notEqual(await sha256(canonicalPlan(f.plan)), await sha256(canonicalPlan({ entries: [{ ...f.entry, new_relevance: "DIRECT" }] })));
    f.db.prepare("UPDATE publications SET normalized_snapshot_json=?").bind(JSON.stringify(f.snapshot, null, 2)).run();
    const r = await call(f); assert.equal(r.status, 409); assert.equal(r.body.stale, 1);
  } finally { f.db.close(); }
});
test("no-op entries do not manufacture audit events", async () => {
  const f = await fixture(); try {
    Object.assign(f.entry, { new_relevance: "POTENTIAL", new_relevance_reason: "old reason", new_ruleset_version: "1.2" }); await repin(f);
    const r = await call(f); assert.equal(r.status, 409); assert.equal(r.body.invalid, 1);
  } finally { f.db.close(); }
});

test("known stale entry blocks the entire invocation before the first ready write", async () => {
  const f = await fixture(); try {
    f.plan.entries.push({ ...f.entry, external_id: "999" }); await repin(f);
    const r = await call(f); assert.equal(r.status, 409); assert.equal(r.body.missing, 1); assert.equal(r.body.applied, 0);
    assert.equal(pub(f.db).relevance, "POTENTIAL"); assert.equal(notifications(f.db)[0].status, "PENDING");
  } finally { f.db.close(); }
});
test("a stale delivery selection cannot claim a suppressed notification", async () => {
  const f = await fixture(); try {
    const selected = notifications(f.db).filter(n => n.status === "PENDING");
    assert.equal((await call(f)).status, 200);
    for (const n of selected) {
      const claim = f.db.prepare("UPDATE notifications SET status='SENDING' WHERE id=? AND status='PENDING'").bind(n.id).run();
      assert.equal(claim.meta.changes, 0);
    }
    assert.equal(notifications(f.db).filter(n => n.status === "SENDING").length, 0);
  } finally { f.db.close(); }
});
test("request size and shape are bounded without reflecting input", async () => {
  const f = await fixture(); try {
    const secret = "private-source-data";
    for (const body of ["{", JSON.stringify({ plan: { entries: Array(1001).fill(f.entry) } }), secret.repeat(70000)]) {
      const r = await worker.fetch(new Request("https://local.invalid/internal/reclassification/preview", {
        method: "POST", headers: { Authorization: "Bearer " + token }, body,
      }), f.env);
      assert.equal(r.status, 400); assert.ok(!(await r.text()).includes(secret));
    }
  } finally { f.db.close(); }
});
