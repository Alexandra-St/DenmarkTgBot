import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { DatabaseSync, type StatementSync } from "node:sqlite";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { execFileSync } from "node:child_process";
import { test } from "node:test";
import { handleIngestionRequest } from "../src/ingestion.ts";
import {
  deliverPendingNotifications,
  formatNotification,
  prepareNotificationQueue,
  previewNotifications,
  sendNotificationCanary,
} from "../src/notifications.ts";
import worker from "../src/index.ts";

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

const now = new Date("2026-10-16T12:00:00.000Z");
const cutoff = "2026-10-15T00:00:00.000Z";

async function fixture(): Promise<LocalD1Fixture> {
  const db = new LocalD1Fixture();
  for (const migration of ["0001_initial_schema.sql", "0002_ingestion.sql", "0003_ingestion_safety.sql", "0004_historical_baseline_gate.sql", "0005_notification_delivery_claim.sql", "0006_notification_canary_once.sql", "0008_source_deletions.sql"]) {
    db.exec(await readFile(new URL("../migrations/" + migration, import.meta.url), "utf8"));
  }
  await db.prepare("INSERT INTO ingestion_runs (run_id, source_id, mode, expected_batches, collection_complete, status, proposed_checkpoints_json, base_checkpoints_json, start_request_hash, created_at, updated_at, completed_at) SELECT '00000000-0000-4000-8000-000000000001', id, 'incremental', 1, 1, 'COMMITTED', '{}', '{}', 'fixture', ?, ?, ? FROM sources WHERE code = 'hoeringsportalen'")
    .bind(now.toISOString(), now.toISOString(), now.toISOString()).run();
  await db.prepare("INSERT INTO users (telegram_id, language, notify_direct, notify_potential, created_at, updated_at) VALUES (1, 'uk', 1, 0, ?, ?), (2, 'ru', 0, 1, ?, ?), (3, 'uk', 0, 0, ?, ?)")
    .bind(now.toISOString(), now.toISOString(), now.toISOString(), now.toISOString(), now.toISOString(), now.toISOString()).run();
  return db;
}

async function addEvent(
  db: LocalD1Fixture,
  options: {
    id: string; source?: string; relevance?: string; eventType?: string;
    eligible?: number; detected?: string; historical?: number; complete?: number;
    url?: string; previous?: string | null; next?: string | null;
  },
): Promise<number> {
  const source = options.source ?? "hoeringsportalen";
  const sourceId = await db.prepare("SELECT id FROM sources WHERE code = ?").bind(source).first<{ id: number }>();
  assert.ok(sourceId);
  const pub = await db.prepare("INSERT INTO publications (source_id, external_id, external_url, original_title, relevance, discovered_at, updated_at, source_complete, historical) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
    .bind(sourceId.id, options.id, options.url ?? "https://hoeringsportalen.dk/Hearing/Details/123", "Test publication " + options.id,
      options.relevance ?? "DIRECT", now.toISOString(), now.toISOString(), options.complete ?? 1, options.historical ?? 0).run();
  const publicationId = Number(pub.meta.last_row_id);
  const event = await db.prepare("INSERT INTO publication_events (publication_id, event_key, event_type, previous_status, new_status, detected_at, notify_eligible) VALUES (?, ?, ?, ?, ?, ?, ?)")
    .bind(publicationId, "ingest:00000000-0000-4000-8000-000000000001:fixture:" + options.id, options.eventType ?? "TITLE_CHANGED",
      options.previous ?? null, options.next ?? null, options.detected ?? "2026-10-15T01:00:00.000Z", options.eligible ?? 1).run();
  return Number(event.meta.last_row_id);
}

test("activation cutoff fails closed when missing, invalid, or not yet reached", async () => {
  const db = await fixture();
  try {
    await addEvent(db, { id: "1" });
    assert.deepEqual(await prepareNotificationQueue({ DB: db as never }, "00000000-0000-4000-8000-000000000001", now), { eligible: false, queued: 0 });
    assert.deepEqual(await prepareNotificationQueue({ DB: db as never, NOTIFICATION_QUEUE_ENABLED: "true", NOTIFICATION_ACTIVATION_CUTOFF: "not-a-date" }, "00000000-0000-4000-8000-000000000001", now), { eligible: false, queued: 0 });
    assert.deepEqual(await prepareNotificationQueue({ DB: db as never, NOTIFICATION_QUEUE_ENABLED: "true", NOTIFICATION_ACTIVATION_CUTOFF: "2026-02-30T00:00:00Z" }, "00000000-0000-4000-8000-000000000001", now), { eligible: false, queued: 0 });
    assert.deepEqual(await prepareNotificationQueue({ DB: db as never, NOTIFICATION_QUEUE_ENABLED: "true", NOTIFICATION_ACTIVATION_CUTOFF: "2026-10-17T00:00:00Z" }, "00000000-0000-4000-8000-000000000001", now), { eligible: false, queued: 0 });
    const rows = await db.prepare("SELECT COUNT(*) AS count FROM notifications").first<{ count: number }>();
    assert.equal(rows?.count, 0);
  } finally { db.close(); }
});

test("delivery-state migration preserves existing notification records", async () => {
  const db = new LocalD1Fixture();
  try {
    for (const migration of ["0001_initial_schema.sql", "0002_ingestion.sql", "0003_ingestion_safety.sql", "0004_historical_baseline_gate.sql"]) {
      db.exec(await readFile(new URL("../migrations/" + migration, import.meta.url), "utf8"));
    }
    const timestamp = now.toISOString();
    await db.prepare("INSERT INTO users (telegram_id, language, created_at, updated_at) VALUES (77, 'uk', ?, ?)").bind(timestamp, timestamp).run();
    await db.prepare("INSERT INTO publications (source_id, external_id, external_url, original_title, relevance, discovered_at, updated_at) SELECT id, 'legacy', 'https://hoeringsportalen.dk/Hearing/Details/77', 'Legacy title', 'DIRECT', ?, ? FROM sources WHERE code = 'hoeringsportalen'")
      .bind(timestamp, timestamp).run();
    await db.prepare("INSERT INTO publication_events (publication_id, event_key, event_type, detected_at) SELECT id, 'legacy-event', 'TITLE_CHANGED', ? FROM publications WHERE external_id = 'legacy'")
      .bind(timestamp).run();
    await db.prepare("INSERT INTO notifications (telegram_id, event_id, status, attempts, created_at) SELECT 77, id, 'PENDING', 0, ? FROM publication_events WHERE event_key = 'legacy-event'")
      .bind(timestamp).run();

    db.exec(await readFile(new URL("../migrations/0005_notification_delivery_claim.sql", import.meta.url), "utf8"));
    const preserved = await db.prepare("SELECT telegram_id, event_id, status, attempts FROM notifications").first<{ telegram_id: number; event_id: number; status: string; attempts: number }>();
    assert.deepEqual({ ...preserved }, { telegram_id: 77, event_id: 1, status: "PENDING", attempts: 0 });
    const table = await db.prepare("SELECT sql FROM sqlite_master WHERE type='table' AND name='notifications'").first<{ sql: string }>();
    assert.match(table?.sql ?? "", /'SENDING'/u);
    const user = await db.prepare("SELECT language, notify_direct, notify_potential FROM users WHERE telegram_id = 77")
      .first<{ language: string; notify_direct: number; notify_potential: number }>();
    assert.deepEqual({ ...user }, { language: "uk", notify_direct: 1, notify_potential: 0 });
    assert.equal((await db.prepare("SELECT COUNT(*) AS count FROM publications").first<{ count: number }>())?.count, 1);
    assert.equal((await db.prepare("SELECT COUNT(*) AS count FROM publication_events").first<{ count: number }>())?.count, 1);
  } finally { db.close(); }
});

test("queue respects relevance preferences, eligibility, history, first-seen, cutoff, and stale-event gates", async () => {
  const db = await fixture();
  try {
    const active = [
      { id: "direct", relevance: "DIRECT" },
      { id: "potential", relevance: "POTENTIAL" },
      { id: "historical", historical: 1 },
      { id: "first-seen", eventType: "FIRST_SEEN" },
      { id: "ineligible", eligible: 0 },
      { id: "before-cutoff", detected: "2026-10-14T23:59:59.000Z" },
      { id: "stale", detected: "2026-10-01T00:00:00.000Z" },
      { id: "incomplete", complete: 0 },
    ];
    for (const item of active) await addEvent(db, item);

    const env = { DB: db as never, NOTIFICATION_QUEUE_ENABLED: "true", NOTIFICATION_ACTIVATION_CUTOFF: cutoff };
    assert.deepEqual(await prepareNotificationQueue(env, "00000000-0000-4000-8000-000000000001", now), { eligible: true, queued: 2 });
    assert.deepEqual(await prepareNotificationQueue(env, "00000000-0000-4000-8000-000000000001", now), { eligible: true, queued: 0 }, "repeated queueing is idempotent");
    const rows = await db.prepare("SELECT u.telegram_id, e.event_type, p.relevance FROM notifications n JOIN users u ON u.telegram_id=n.telegram_id JOIN publication_events e ON e.id=n.event_id JOIN publications p ON p.id=e.publication_id ORDER BY u.telegram_id")
      .all<{ telegram_id: number; event_type: string; relevance: string }>();
    assert.deepEqual(rows.results.map((row) => ({ ...row })), [
      { telegram_id: 1, event_type: "TITLE_CHANGED", relevance: "DIRECT" },
      { telegram_id: 2, event_type: "TITLE_CHANGED", relevance: "POTENTIAL" },
    ]);
  } finally { db.close(); }
});

test("queue requires its own enable flag and a committed incremental run", async () => {
  const db = await fixture();
  try {
    await addEvent(db, { id: "gated" });
    const config = { DB: db as never, NOTIFICATION_ACTIVATION_CUTOFF: cutoff };
    const runId = "00000000-0000-4000-8000-000000000001";
    assert.deepEqual(await prepareNotificationQueue(config, runId, now), { eligible: false, queued: 0 });
    const enabled = { ...config, NOTIFICATION_QUEUE_ENABLED: "true" };
    await db.prepare("UPDATE ingestion_runs SET status = 'OPEN' WHERE run_id = ?").bind(runId).run();
    assert.deepEqual(await prepareNotificationQueue(enabled, runId, now), { eligible: false, queued: 0 });
    await db.prepare("UPDATE ingestion_runs SET status = 'COMMITTED', mode = 'historical' WHERE run_id = ?").bind(runId).run();
    assert.deepEqual(await prepareNotificationQueue(enabled, runId, now), { eligible: false, queued: 0 });
    const count = await db.prepare("SELECT COUNT(*) AS count FROM notifications").first<{ count: number }>();
    assert.equal(count?.count, 0);
  } finally { db.close(); }
});

test("confirmed source deletion creates no queue item and suppresses pending delivery", async () => {
  const db = await fixture();
  try {
    const eventId = await addEvent(db, { id: "deleted-pending", source: "folketinget", url: "https://oda.ft.dk/api/Sag(123)" });
    await db.prepare("UPDATE publications SET source_deleted=1 WHERE external_id='deleted-pending'").run();
    await db.prepare("INSERT INTO notifications (telegram_id, event_id, status, attempts, created_at) VALUES (1, ?, 'PENDING', 0, ?)")
      .bind(eventId, now.toISOString()).run();
    const queue = await prepareNotificationQueue(
      { DB: db as never, NOTIFICATION_QUEUE_ENABLED: "true", NOTIFICATION_ACTIVATION_CUTOFF: cutoff },
      "00000000-0000-4000-8000-000000000001", now,
    );
    assert.deepEqual(queue, { eligible: true, queued: 0 });
    const preview = await previewNotifications({ DB: db as never, NOTIFICATION_ACTIVATION_CUTOFF: cutoff }, now);
    assert.equal(preview.eligible_events, 0);
    let sends = 0;
    const delivery = await deliverPendingNotifications(
      { DB: db as never, NOTIFICATION_DELIVERY_ENABLED: "true", NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF: cutoff },
      { async sendMessage() { sends += 1; } }, now,
    );
    assert.deepEqual(delivery, { enabled: true, sent: 0, failed: 0 });
    assert.equal(sends, 0);
    assert.equal((await db.prepare("SELECT status FROM notifications WHERE event_id=?").bind(eventId).first<{ status: string }>())?.status, "PENDING");
  } finally { db.close(); }
});

test("preview is aggregate-only, read-only, and fails closed without activation", async () => {
  const db = await fixture();
  try {
    await addEvent(db, { id: "preview-direct", relevance: "DIRECT" });
    await addEvent(db, { id: "preview-potential", relevance: "POTENTIAL" });
    await addEvent(db, { id: "preview-history", historical: 1 });
    await addEvent(db, { id: "preview-stale", detected: "2026-10-02T00:00:00.000Z" });
    const before = await db.prepare("SELECT status, COUNT(*) AS count FROM notifications GROUP BY status").all<{ status: string; count: number }>();
    const closed = await previewNotifications({ DB: db as never }, now);
    assert.equal(closed.activation_configured, false);
    assert.equal(closed.eligible_events, 0);
    const preview = await previewNotifications({ DB: db as never, NOTIFICATION_ACTIVATION_CUTOFF: cutoff }, now);
    assert.deepEqual(preview, {
      activation_configured: true, eligible_events: 2, estimated_recipients: 2,
      direct_events: 1, potential_events: 1, direct_recipients: 1, potential_recipients: 1,
      suppressed_historical_events: 1, suppressed_stale_events: 1,
    });
    assert.equal(JSON.stringify(preview).includes("telegram_id"), false);
    const after = await db.prepare("SELECT status, COUNT(*) AS count FROM notifications GROUP BY status").all<{ status: string; count: number }>();
    assert.deepEqual(after.results.map((row) => ({ ...row })), before.results.map((row) => ({ ...row })));
    assert.equal((await db.prepare("SELECT COUNT(*) AS count FROM notifications").first<{ count: number }>())?.count, 0);
  } finally { db.close(); }
});

test("notification preview route uses ingestion authorization and never exposes identities", async () => {
  const db = await fixture();
  try {
    const env = { DB: db as never, INGEST_API_TOKEN: "local-only", NOTIFICATION_ACTIVATION_CUTOFF: new Date(Date.now() - 60_000).toISOString() };
    const fetch = worker.fetch as unknown as (request: Request, env: typeof env) => Promise<Response>;
    const unauthorized = await fetch(new Request("https://local.test/internal/notifications/preview"), env);
    assert.equal(unauthorized.status, 401);
    const queueUnauthenticated = await fetch(new Request("https://local.test/internal/notifications/prepare", { method: "POST" }), env);
    assert.equal(queueUnauthenticated.status, 401);
    const deliveryUnauthenticated = await fetch(new Request("https://local.test/internal/notifications/deliver", { method: "POST" }), env);
    assert.equal(deliveryUnauthenticated.status, 401);
    const authorized = await fetch(new Request("https://local.test/internal/notifications/preview", {
      headers: { Authorization: "Bearer local-only" },
    }), env);
    assert.equal(authorized.status, 200);
    const body = await authorized.json() as Record<string, unknown>;
    assert.equal(body.activation_configured, true);
    assert.equal(JSON.stringify(body).includes("telegram_id"), false);
    assert.equal((await db.prepare("SELECT COUNT(*) AS count FROM notifications").first<{ count: number }>())?.count, 0);
  } finally { db.close(); }
});

test("delivery route is protected and disabled unless separately enabled with its cutoff", async () => {
  const db = await fixture();
  try {
    const token = "route-token";
    const fetch = worker.fetch as unknown as (request: Request, env: Record<string, unknown>) => Promise<Response>;
    const env = { DB: db as never, INGEST_API_TOKEN: token, TELEGRAM_BOT_TOKEN: "mock-only" };
    const unauthorized = await fetch(new Request("https://local.test/internal/notifications/deliver", { method: "POST" }), env);
    assert.equal(unauthorized.status, 401);
    const disabled = await fetch(new Request("https://local.test/internal/notifications/deliver", {
      method: "POST", headers: { Authorization: "Bearer " + token },
    }), env);
    assert.equal(disabled.status, 200);
    assert.deepEqual(await disabled.json(), { enabled: false, sent: 0, failed: 0 });
  } finally { db.close(); }
});

test("canary sends one fixed message only to the explicitly allowlisted recipient", async () => {
  const db = await fixture();
  try {
    const env = {
      DB: db as never,
      NOTIFICATION_CANARY_ENABLED: "true",
      NOTIFICATION_CANARY_ACTIVATION_CUTOFF: cutoff,
      NOTIFICATION_CANARY_TELEGRAM_ID: "2",
    };
    const deliveries: Array<{ id: number; text: string }> = [];
    const telegram = { async sendMessage(id: number, text: string) { deliveries.push({ id, text }); } };
    const request = { confirmation: "SEND_ONE_CANARY_MESSAGE", request_id: "00000000-0000-4000-8000-000000000099", recipient_id: 2 };
    assert.deepEqual(await sendNotificationCanary(env, telegram, request, now), { status: "sent" });
    assert.deepEqual(deliveries, [{ id: 2, text: "🧪 DenmarkBot — тестовое уведомление" }]);
    assert.deepEqual(await sendNotificationCanary(env, telegram, request, now), { status: "already_used" });
    assert.deepEqual(await sendNotificationCanary(env, telegram, { ...request, request_id: "00000000-0000-4000-8000-000000000098" }, now), { status: "already_used" });
    assert.equal(deliveries.length, 1, "repeated requests cannot duplicate the canary message");
    assert.equal((await db.prepare("SELECT COUNT(*) AS count FROM notifications").first<{ count: number }>())?.count, 0);
    assert.equal((await db.prepare("SELECT COUNT(*) AS count FROM publications").first<{ count: number }>())?.count, 0);
    assert.equal((await db.prepare("SELECT COUNT(*) AS count FROM publication_events").first<{ count: number }>())?.count, 0);
    const attempt = await db.prepare("SELECT telegram_id, status FROM notification_canary_attempts WHERE singleton_id = 1")
      .first<{ telegram_id: number; status: string }>();
    assert.deepEqual({ ...attempt }, { telegram_id: 2, status: "SENT" });
  } finally { db.close(); }
});

test("concurrent canary requests atomically claim the single delivery slot", async () => {
  const db = await fixture();
  try {
    const env = { DB: db as never, NOTIFICATION_CANARY_ENABLED: "true", NOTIFICATION_CANARY_ACTIVATION_CUTOFF: cutoff, NOTIFICATION_CANARY_TELEGRAM_ID: "2" };
    let calls = 0;
    const telegram = { async sendMessage() { calls += 1; await Promise.resolve(); } };
    const request = (requestId: string) => ({ confirmation: "SEND_ONE_CANARY_MESSAGE", request_id: requestId, recipient_id: 2 });
    const results = await Promise.all([
      sendNotificationCanary(env, telegram, request("00000000-0000-4000-8000-000000000091"), now),
      sendNotificationCanary(env, telegram, request("00000000-0000-4000-8000-000000000092"), now),
    ]);
    assert.deepEqual(results.map((result) => result.status).sort(), ["already_used", "sent"]);
    assert.equal(calls, 1);
  } finally { db.close(); }
});

test("canary fails closed for missing, invalid, expired, or mismatched configuration and confirmation", async () => {
  const db = await fixture();
  try {
    const request = { confirmation: "SEND_ONE_CANARY_MESSAGE", request_id: "00000000-0000-4000-8000-000000000099", recipient_id: 2 };
    let calls = 0;
    const telegram = { async sendMessage() { calls += 1; } };
    const configs = [
      { env: {}, requestedRecipient: 2 },
      { env: { NOTIFICATION_CANARY_ENABLED: "false", NOTIFICATION_CANARY_ACTIVATION_CUTOFF: cutoff, NOTIFICATION_CANARY_TELEGRAM_ID: "2" }, requestedRecipient: 2 },
      { env: { NOTIFICATION_CANARY_ENABLED: "true", NOTIFICATION_CANARY_ACTIVATION_CUTOFF: "invalid", NOTIFICATION_CANARY_TELEGRAM_ID: "2" }, requestedRecipient: 2 },
      { env: { NOTIFICATION_CANARY_ENABLED: "true", NOTIFICATION_CANARY_ACTIVATION_CUTOFF: "2026-10-17T00:00:00.000Z", NOTIFICATION_CANARY_TELEGRAM_ID: "2" }, requestedRecipient: 2 },
      { env: { NOTIFICATION_CANARY_ENABLED: "true", NOTIFICATION_CANARY_ACTIVATION_CUTOFF: cutoff, NOTIFICATION_CANARY_TELEGRAM_ID: "" }, requestedRecipient: 2 },
      { env: { NOTIFICATION_CANARY_ENABLED: "true", NOTIFICATION_CANARY_ACTIVATION_CUTOFF: cutoff, NOTIFICATION_CANARY_TELEGRAM_ID: "2" }, requestedRecipient: 3 },
    ];
    for (const { env, requestedRecipient } of configs) {
      assert.deepEqual(await sendNotificationCanary({ DB: db as never, ...env } as never, telegram,
        { ...request, recipient_id: requestedRecipient }, now), { status: "blocked" });
    }
    assert.deepEqual(await sendNotificationCanary({
      DB: db as never, NOTIFICATION_CANARY_ENABLED: "true", NOTIFICATION_CANARY_ACTIVATION_CUTOFF: cutoff,
      NOTIFICATION_CANARY_TELEGRAM_ID: "2",
    }, telegram, { ...request, confirmation: "" }, now), { status: "blocked" });
    assert.equal(calls, 0);
    assert.equal((await db.prepare("SELECT COUNT(*) AS count FROM notification_canary_attempts").first<{ count: number }>())?.count, 0);
  } finally { db.close(); }
});

test("canary route remains authenticated and inactive without its dedicated gates", async () => {
  const db = await fixture();
  try {
    const token = "canary-route-local";
    const fetch = worker.fetch as unknown as (request: Request, env: Record<string, unknown>) => Promise<Response>;
    const env = { DB: db as never, INGEST_API_TOKEN: token, TELEGRAM_BOT_TOKEN: "mock-only" };
    const body = JSON.stringify({ confirmation: "SEND_ONE_CANARY_MESSAGE", request_id: "00000000-0000-4000-8000-000000000099", recipient_id: 2 });
    const unauthenticated = await fetch(new Request("https://local.test/internal/notifications/canary", { method: "POST", body }), env);
    assert.equal(unauthenticated.status, 401);
    const disabled = await fetch(new Request("https://local.test/internal/notifications/canary", {
      method: "POST", headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" }, body,
    }), env);
    assert.equal(disabled.status, 200);
    assert.deepEqual(await disabled.json(), { status: "blocked" });
    assert.equal((await db.prepare("SELECT COUNT(*) AS count FROM notification_canary_attempts").first<{ count: number }>())?.count, 0);

    const originalFetch = globalThis.fetch;
    const telegramCalls: Array<{ url: string; body: Record<string, unknown> }> = [];
    globalThis.fetch = async (input, init) => {
      telegramCalls.push({ url: String(input), body: JSON.parse(String(init?.body)) as Record<string, unknown> });
      return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } });
    };
    try {
      const activeEnv = {
        ...env,
        NOTIFICATION_CANARY_ENABLED: "true",
        NOTIFICATION_CANARY_ACTIVATION_CUTOFF: cutoff,
        NOTIFICATION_CANARY_TELEGRAM_ID: "2",
      };
      const regularDelivery = await fetch(new Request("https://local.test/internal/notifications/deliver", {
        method: "POST", headers: { Authorization: "Bearer " + token },
      }), activeEnv);
      assert.deepEqual(await regularDelivery.json(), { enabled: false, sent: 0, failed: 0 });
      assert.equal(telegramCalls.length, 0, "canary gates do not enable production-wide delivery");
      const authorizedRequest = () => new Request("https://local.test/internal/notifications/canary", {
        method: "POST", headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" }, body,
      });
      const sent = await fetch(authorizedRequest(), activeEnv);
      assert.deepEqual(await sent.json(), { status: "sent" });
      const repeated = await fetch(authorizedRequest(), activeEnv);
      assert.deepEqual(await repeated.json(), { status: "already_used" });
      assert.equal(telegramCalls.length, 1);
      assert.match(telegramCalls[0].url, /^https:\/\/api\.telegram\.org\/botmock-only\/sendMessage$/u);
      assert.deepEqual(telegramCalls[0].body, { chat_id: 2, text: "🧪 DenmarkBot — тестовое уведомление" });
      assert.equal((await db.prepare("SELECT COUNT(*) AS count FROM publications").first<{ count: number }>())?.count, 0);
      assert.equal((await db.prepare("SELECT COUNT(*) AS count FROM publication_events").first<{ count: number }>())?.count, 0);
      assert.equal((await db.prepare("SELECT COUNT(*) AS count FROM notifications").first<{ count: number }>())?.count, 0);
    } finally {
      globalThis.fetch = originalFetch;
    }
  } finally { db.close(); }
});

test("ambiguous canary failure is terminal and consumes the single attempt", async () => {
  const db = await fixture();
  try {
    const env = { DB: db as never, NOTIFICATION_CANARY_ENABLED: "true", NOTIFICATION_CANARY_ACTIVATION_CUTOFF: cutoff, NOTIFICATION_CANARY_TELEGRAM_ID: "2" };
    let calls = 0;
    const telegram = { async sendMessage() { calls += 1; throw new Error("ambiguous timeout"); } };
    const request = { confirmation: "SEND_ONE_CANARY_MESSAGE", request_id: "00000000-0000-4000-8000-000000000099", recipient_id: 2 };
    assert.deepEqual(await sendNotificationCanary(env, telegram, request, now), { status: "failed" });
    assert.deepEqual(await sendNotificationCanary(env, telegram, request, now), { status: "already_used" });
    assert.equal(calls, 1);
    const attempt = await db.prepare("SELECT status, last_error FROM notification_canary_attempts WHERE singleton_id = 1")
      .first<{ status: string; last_error: string }>();
    assert.deepEqual({ ...attempt }, { status: "FAILED", last_error: "telegram_delivery_ambiguous_or_failed" });
  } finally { db.close(); }
});

test("localized message includes change, relevance, title, source, official URL, and disclaimer", () => {
  const common = {
    id: 1, telegram_id: 1, event_type: "STATUS_CHANGED", previous_status: "Open", new_status: "Closed",
    relevance: "DIRECT" as const, title: "Danish bill", source_code: "hoeringsportalen",
    source_name: "Høringsportalen", external_url: "https://hoeringsportalen.dk/Hearing/Details/123",
  };
  const uk = formatNotification({ ...common, language: "uk" });
  assert.match(uk ?? "", /🟢 DIRECT/u);
  assert.match(uk ?? "", /статус/u);
  assert.match(uk ?? "", /Було: Open/u);
  assert.match(uk ?? "", /Стало: Closed/u);
  assert.match(uk ?? "", /Danish bill/u);
  assert.match(uk ?? "", /Høringsportalen/u);
  assert.match(uk ?? "", /https:\/\/hoeringsportalen\.dk\/Hearing\/Details\/123/u);
  assert.match(uk ?? "", /не юридична консультація/u);

  const ru = formatNotification({ ...common, language: "ru", relevance: "POTENTIAL" });
  assert.match(ru ?? "", /🟡 POTENTIAL/u);
  assert.match(ru ?? "", /Изменился статус/u);
  assert.match(ru ?? "", /Было: Open/u);
  assert.match(ru ?? "", /Стало: Closed/u);
  assert.match(ru ?? "", /не юридическая консультация/u);
});

test("invalid official URLs and unknown change types fail closed", () => {
  const event = {
    id: 1, telegram_id: 1, language: "uk" as const, event_type: "TITLE_CHANGED",
    previous_status: null, new_status: null, relevance: "DIRECT" as const, title: "Title",
    source_code: "hoeringsportalen", source_name: "Høringsportalen", external_url: "https://example.com/item",
  };
  assert.equal(formatNotification(event), null);
  assert.equal(formatNotification({ ...event, external_url: "https://hoeringsportalen.dk/item", event_type: "UNRECOGNIZED" }), null);
});

test("delivery remains disabled by default; success is recorded once through a mock client", async () => {
  const db = await fixture();
  try {
    const eventId = await addEvent(db, { id: "deliver" });
    await db.prepare("INSERT INTO notifications (telegram_id, event_id, status, created_at) VALUES (1, ?, 'PENDING', ?)")
      .bind(eventId, now.toISOString()).run();
    let calls = 0;
    const mock = { async sendMessage(_id: number, text: string) { calls += 1; assert.match(text, /Test publication deliver/u); } };
    assert.deepEqual(await deliverPendingNotifications({ DB: db as never }, mock), { enabled: false, sent: 0, failed: 0 });
    assert.deepEqual(await deliverPendingNotifications({
      DB: db as never, NOTIFICATION_DELIVERY_ENABLED: "true", NOTIFICATION_ACTIVATION_CUTOFF: cutoff,
    }, mock, now), { enabled: false, sent: 0, failed: 0 }, "queue cutoff does not activate delivery");
    assert.deepEqual(await deliverPendingNotifications({
      DB: db as never, NOTIFICATION_DELIVERY_ENABLED: "true", NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF: "invalid",
    }, mock, now), { enabled: false, sent: 0, failed: 0 });
    assert.deepEqual(await deliverPendingNotifications({
      DB: db as never, NOTIFICATION_DELIVERY_ENABLED: "true", NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF: "2026-10-17T00:00:00Z",
    }, mock, now), { enabled: false, sent: 0, failed: 0 });
    assert.equal(calls, 0);

    const result = await deliverPendingNotifications({
      DB: db as never, NOTIFICATION_DELIVERY_ENABLED: "true", NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF: cutoff,
    }, mock, now);
    assert.deepEqual(result, { enabled: true, sent: 1, failed: 0 });
    assert.equal(calls, 1);
    const row = await db.prepare("SELECT status, attempts FROM notifications WHERE event_id = ?").bind(eventId).first<{ status: string; attempts: number }>();
    assert.deepEqual({ ...row }, { status: "SENT", attempts: 1 });
  } finally { db.close(); }
});

test("Telegram timeout/error is terminal and is not retried automatically", async () => {
  const db = await fixture();
  try {
    const eventId = await addEvent(db, { id: "timeout" });
    await db.prepare("INSERT INTO notifications (telegram_id, event_id, status, created_at) VALUES (1, ?, 'PENDING', ?)")
      .bind(eventId, now.toISOString()).run();
    let calls = 0;
    const failing = { async sendMessage() { calls += 1; throw new Error("timeout"); } };
    const env = { DB: db as never, NOTIFICATION_DELIVERY_ENABLED: "true", NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF: cutoff };
    assert.deepEqual(await deliverPendingNotifications(env, failing, now), { enabled: true, sent: 0, failed: 1 });
    assert.deepEqual(await deliverPendingNotifications(env, failing, now), { enabled: true, sent: 0, failed: 0 });
    assert.equal(calls, 1);
    const row = await db.prepare("SELECT status, attempts, last_error FROM notifications WHERE event_id = ?").bind(eventId).first<{ status: string; attempts: number; last_error: string }>();
    assert.deepEqual({ ...row }, { status: "FAILED", attempts: 1, last_error: "telegram_delivery_ambiguous_or_failed" });
  } finally { db.close(); }
});

for (const [name, error] of [
  ["Telegram rejection", new Error("Telegram rejected the request")],
  ["network failure", new TypeError("network unavailable")],
] as const) {
  test(name + " is terminal and is never resent", async () => {
    const db = await fixture();
    try {
      const eventId = await addEvent(db, { id: name.replaceAll(" ", "-") });
      await db.prepare("INSERT INTO notifications (telegram_id, event_id, status, created_at) VALUES (1, ?, 'PENDING', ?)")
        .bind(eventId, now.toISOString()).run();
      let calls = 0;
      const env = { DB: db as never, NOTIFICATION_DELIVERY_ENABLED: "true", NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF: cutoff };
      const mock = { async sendMessage() { calls += 1; throw error; } };
      assert.deepEqual(await deliverPendingNotifications(env, mock, now), { enabled: true, sent: 0, failed: 1 });
      assert.deepEqual(await deliverPendingNotifications(env, mock, now), { enabled: true, sent: 0, failed: 0 });
      assert.equal(calls, 1);
    } finally { db.close(); }
  });
}

test("concurrent dispatchers claim a notification once; invalid URLs are never sent", async () => {
  const db = await fixture();
  try {
    const eventId = await addEvent(db, { id: "concurrent" });
    await db.prepare("INSERT INTO notifications (telegram_id, event_id, status, created_at) VALUES (1, ?, 'PENDING', ?)")
      .bind(eventId, now.toISOString()).run();
    let calls = 0;
    const mock = { async sendMessage() { calls += 1; } };
    const env = { DB: db as never, NOTIFICATION_DELIVERY_ENABLED: "true", NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF: cutoff };
    const results = await Promise.all([
      deliverPendingNotifications(env, mock, now),
      deliverPendingNotifications(env, mock, now),
    ]);
    assert.equal(calls, 1);
    assert.equal(results.reduce((total, result) => total + result.sent, 0), 1);
    const row = await db.prepare("SELECT status, attempts, last_error FROM notifications WHERE event_id = ?")
      .bind(eventId).first<{ status: string; attempts: number; last_error: string }>();
    assert.equal(row?.status, "SENT");

    const invalidId = await addEvent(db, { id: "bad-url", url: "https://example.com/not-official" });
    await db.prepare("INSERT INTO notifications (telegram_id, event_id, status, created_at) VALUES (1, ?, 'PENDING', ?)")
      .bind(invalidId, now.toISOString()).run();
    const invalidResult = await deliverPendingNotifications(env, mock, now);
    assert.equal(invalidResult.failed, 1);
    assert.equal(calls, 1, "invalid official URL is never sent");
  } finally { db.close(); }
});

test("a row left SENDING by process interruption is not automatically resent", async () => {
  const db = await fixture();
  try {
    const eventId = await addEvent(db, { id: "interrupted" });
    await db.prepare("INSERT INTO notifications (telegram_id, event_id, status, attempts, created_at) VALUES (1, ?, 'SENDING', 1, ?)")
      .bind(eventId, now.toISOString()).run();
    let calls = 0;
    const result = await deliverPendingNotifications({
      DB: db as never, NOTIFICATION_DELIVERY_ENABLED: "true", NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF: cutoff,
    }, { async sendMessage() { calls += 1; } }, now);
    assert.deepEqual(result, { enabled: true, sent: 0, failed: 0 });
    assert.equal(calls, 0);
    const row = await db.prepare("SELECT status, attempts FROM notifications WHERE event_id = ?")
      .bind(eventId).first<{ status: string; attempts: number }>();
    assert.deepEqual({ ...row }, { status: "SENDING", attempts: 1 });
  } finally { db.close(); }
});

test("local end-to-end: historical baseline, incremental changes, preference queue, and mock delivery", async () => {
  const directory = await mkdtemp(join(tmpdir(), "denmarktgbot-notification-e2e-"));
  const db = new LocalD1Fixture(join(directory, "temporary-d1.sqlite"));
  const token = "local-e2e-only-token";
  const env = { DB: db as never, INGEST_API_TOKEN: token };
  const invoke = async (body: Record<string, unknown>) => {
    const response = await handleIngestionRequest(new Request("https://local.test/internal/ingest", {
      method: "POST",
      headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }), env as never);
    return { status: response.status, body: await response.json() as Record<string, unknown> };
  };
  const cursor = (timestamp: string, id: number) => ({ timestamp, id });
  const checkpoints = (timestamp: string, id: number) => ({
    immigration_ministry: cursor(timestamp, id),
    immigration_category: cursor(timestamp, id),
  });
  const rawPublication = (
    id: number,
    changed = false,
  ) => {
    const title = (id === 101 ? "Forslag om Ukrainesærloven" : id === 102 ? "Udlændingeloven om opholdstilladelser" : "Vejtrafikregler " + id)
      + (changed ? " — updated" : "");
    return {
      external_id: String(id), title,
      url: "https://hoeringsportalen.dk/Hearing/Details/" + id,
      content_text: changed ? "Revised consultation text" : "Original consultation text",
      source_updated_at: changed ? "2026-10-16T10:00:00.000Z" : "2026-09-01T10:00:00.000Z",
      updated_at: changed ? "2026-10-16T10:00:00Z" : "2026-09-01T10:00:00Z",
      updated_at_raw: changed ? "2026-10-16T10:00:00Z" : "2026-09-01T10:00:00Z",
      author: "Official ministry", feed_codes: ["immigration_ministry", "immigration_category"],
      complete: true,
    };
  };
  const classify = (publications: Array<Record<string, unknown>>, mode: "historical" | "incremental") => {
    const script = "import json,sys; from collectors.ingestion_client import prepare_records; print(json.dumps(prepare_records(json.load(sys.stdin), 'hoeringsportalen', '" + mode + "')))";
    return JSON.parse(execFileSync("python3", ["-c", script], {
      cwd: new URL("../../", import.meta.url).pathname,
      input: JSON.stringify({ publications }),
      encoding: "utf8",
    })) as Array<Record<string, unknown>>;
  };
  const run = async (
    runId: string,
    mode: "historical" | "incremental",
    records: Array<Record<string, unknown>>,
    checkpointSet: Record<string, unknown>,
  ) => {
    const started = await invoke({
      action: "start", run_id: runId, source: "hoeringsportalen", mode,
      expected_batches: 1, collection_complete: true, proposed_checkpoints: checkpointSet,
    });
    assert.ok(started.status === 201 || started.body.idempotent === true, JSON.stringify(started.body));
    const submitted = await invoke({ action: "batch", run_id: runId, batch_index: 0, records });
    assert.equal(submitted.status, 201, JSON.stringify(submitted.body));
    let committed = await invoke({ action: "commit", run_id: runId });
    for (let attempt = 0; committed.body.status !== "COMMITTED" && attempt < 5; attempt += 1) {
      assert.equal(committed.body.status, "COMMITTING");
      committed = await invoke({ action: "commit", run_id: runId });
    }
    assert.equal(committed.body.status, "COMMITTED", JSON.stringify(committed.body));
  };

  try {
    for (const migration of ["0001_initial_schema.sql", "0002_ingestion.sql", "0003_ingestion_safety.sql", "0004_historical_baseline_gate.sql", "0005_notification_delivery_claim.sql", "0006_notification_canary_once.sql", "0007_snapshot_ingestion_state.sql", "0008_source_deletions.sql"]) {
      db.exec(await readFile(new URL("../migrations/" + migration, import.meta.url), "utf8"));
    }
    const timestamp = new Date().toISOString();
    await db.prepare("INSERT INTO users (telegram_id, language, notify_direct, notify_potential, created_at, updated_at) VALUES (1001, 'uk', 1, 0, ?, ?), (1002, 'ru', 0, 1, ?, ?), (1003, 'uk', 0, 0, ?, ?)")
      .bind(timestamp, timestamp, timestamp, timestamp, timestamp, timestamp).run();

    const historicalRaw = Array.from({ length: 25 }, (_, index) => rawPublication(101 + index));
    const historical = classify(historicalRaw, "historical");
    assert.deepEqual(historical.slice(0, 3).map((row) => row.relevance), ["DIRECT", "POTENTIAL", "IRRELEVANT"]);
    await run("00000000-0000-4000-8000-000000000101", "historical", historical,
      checkpoints("2026-09-01T10:00:00.000Z", 125));
    const baseline = await db.prepare("SELECT COUNT(*) AS count FROM source_ingestion_baselines").first<{ count: number }>();
    const historicalRows = await db.prepare("SELECT COUNT(*) AS count FROM publications WHERE historical = 1").first<{ count: number }>();
    const historicalEvents = await db.prepare("SELECT COUNT(*) AS count FROM publication_events WHERE event_type = 'FIRST_SEEN' AND notify_eligible = 0").first<{ count: number }>();
    assert.equal(baseline?.count, 1);
    assert.equal(historicalRows?.count, 25);
    assert.equal(historicalEvents?.count, 25);

    const activationCutoff = new Date(Date.now() - 5_000).toISOString();
    const incrementalRaw = historicalRaw.map((item, index) => index < 2
      ? rawPublication(Number(item.external_id), true)
      : item);
    const incremental = classify(incrementalRaw, "incremental");
    await run("00000000-0000-4000-8000-000000000102", "incremental", incremental,
      checkpoints("2026-10-16T10:00:00.000Z", 125));
    const semanticEvents = await db.prepare("SELECT event_type, COUNT(*) AS count FROM publication_events WHERE event_type != 'FIRST_SEEN' GROUP BY event_type ORDER BY event_type")
      .all<{ event_type: string; count: number }>();
    assert.deepEqual(semanticEvents.results.map((row) => ({ ...row })), [
      { event_type: "CONTENT_CHANGED", count: 2 },
      { event_type: "TITLE_CHANGED", count: 2 },
    ]);

    const envWithCutoff = {
      DB: db as never,
      NOTIFICATION_QUEUE_ENABLED: "true",
      NOTIFICATION_ACTIVATION_CUTOFF: activationCutoff,
      NOTIFICATION_DELIVERY_ENABLED: "true",
      NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF: activationCutoff,
    };
    assert.deepEqual(await prepareNotificationQueue({ DB: db as never }, "00000000-0000-4000-8000-000000000102", new Date()), { eligible: false, queued: 0 });
    assert.deepEqual(await prepareNotificationQueue(envWithCutoff, "00000000-0000-4000-8000-000000000102", new Date()), { eligible: true, queued: 4 });
    assert.deepEqual(await prepareNotificationQueue(envWithCutoff, "00000000-0000-4000-8000-000000000102", new Date()), { eligible: true, queued: 0 });
    const candidateRows = await db.prepare("SELECT u.telegram_id, u.language, p.relevance, e.event_type FROM notifications n JOIN users u ON u.telegram_id=n.telegram_id JOIN publication_events e ON e.id=n.event_id JOIN publications p ON p.id=e.publication_id ORDER BY u.telegram_id, e.event_type")
      .all<{ telegram_id: number; language: string; relevance: string; event_type: string }>();
    assert.equal(candidateRows.results.length, 4);
    assert.ok(candidateRows.results.filter((row) => row.telegram_id === 1001).every((row) => row.language === "uk" && row.relevance === "DIRECT"));
    assert.ok(candidateRows.results.filter((row) => row.telegram_id === 1002).every((row) => row.language === "ru" && row.relevance === "POTENTIAL"));
    assert.equal(candidateRows.results.some((row) => row.telegram_id === 1003), false);

    const sent: Array<{ chatId: number; text: string; keyboard?: unknown }> = [];
    const delivery = await deliverPendingNotifications(envWithCutoff, {
      async sendMessage(chatId, text, keyboard) { sent.push({ chatId, text, keyboard }); },
    }, new Date());
    assert.deepEqual(delivery, { enabled: true, sent: 4, failed: 0 });
    assert.equal(sent.length, 4);
    assert.equal(sent.filter((item) => item.chatId === 1001 && item.text.includes("не юридична консультація")).length, 2);
    assert.equal(sent.filter((item) => item.chatId === 1002 && item.text.includes("не юридическая консультация")).length, 2);
    assert.ok(sent.every((item) => item.text.includes("https://hoeringsportalen.dk/Hearing/Details/")));
    assert.ok(sent.every((item) => JSON.stringify(item.keyboard).includes('"callback_data":"notification:main"')));
    const states = await db.prepare("SELECT status, COUNT(*) AS count FROM notifications GROUP BY status").all<{ status: string; count: number }>();
    assert.deepEqual(states.results.map((row) => ({ ...row })), [{ status: "SENT", count: 4 }]);
    const oldFirstSeenNotifications = await db.prepare("SELECT COUNT(*) AS count FROM notifications n JOIN publication_events e ON e.id=n.event_id WHERE e.event_type='FIRST_SEEN'")
      .first<{ count: number }>();
    assert.equal(oldFirstSeenNotifications?.count, 0);

    const eventCountBeforeRepeat = (await db.prepare("SELECT COUNT(*) AS count FROM publication_events").first<{ count: number }>())?.count;
    await run("00000000-0000-4000-8000-000000000103", "incremental", incremental,
      checkpoints("2026-10-16T10:00:00.000Z", 125));
    const eventCountAfterRepeat = (await db.prepare("SELECT COUNT(*) AS count FROM publication_events").first<{ count: number }>())?.count;
    assert.equal(eventCountAfterRepeat, eventCountBeforeRepeat, "unchanged snapshots create no semantic events");
    assert.deepEqual(await prepareNotificationQueue(envWithCutoff, "00000000-0000-4000-8000-000000000103", new Date()), { eligible: true, queued: 0 });
    const repeatedDelivery = await deliverPendingNotifications(envWithCutoff, {
      async sendMessage() { throw new Error("no new event should be delivered"); },
    }, new Date());
    assert.deepEqual(repeatedDelivery, { enabled: true, sent: 0, failed: 0 });
  } finally {
    db.close();
    await rm(directory, { recursive: true, force: true });
  }
});
