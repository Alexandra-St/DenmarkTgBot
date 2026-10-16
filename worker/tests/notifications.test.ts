import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { DatabaseSync, type StatementSync } from "node:sqlite";
import { test } from "node:test";
import {
  deliverPendingNotifications,
  formatNotification,
  prepareNotificationQueue,
} from "../src/notifications.ts";

class LocalD1Fixture {
  private readonly sqlite = new DatabaseSync(":memory:");
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
    };
  }
  close(): void { this.sqlite.close(); }
}

const now = new Date("2026-10-16T12:00:00.000Z");
const cutoff = "2026-10-15T00:00:00.000Z";

async function fixture(): Promise<LocalD1Fixture> {
  const db = new LocalD1Fixture();
  for (const migration of ["0001_initial_schema.sql", "0002_ingestion.sql", "0003_ingestion_safety.sql", "0004_historical_baseline_gate.sql", "0005_notification_delivery_claim.sql"]) {
    db.exec(await readFile(new URL("../migrations/" + migration, import.meta.url), "utf8"));
  }
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
    .bind(publicationId, "event-" + options.id, options.eventType ?? "TITLE_CHANGED",
      options.previous ?? null, options.next ?? null, options.detected ?? "2026-10-15T01:00:00.000Z", options.eligible ?? 1).run();
  return Number(event.meta.last_row_id);
}

test("activation cutoff fails closed when missing, invalid, or not yet reached", async () => {
  const db = await fixture();
  try {
    await addEvent(db, { id: "1" });
    assert.deepEqual(await prepareNotificationQueue({ DB: db as never }, now), { eligible: false, queued: 0 });
    assert.deepEqual(await prepareNotificationQueue({ DB: db as never, NOTIFICATION_ACTIVATION_CUTOFF: "not-a-date" }, now), { eligible: false, queued: 0 });
    assert.deepEqual(await prepareNotificationQueue({ DB: db as never, NOTIFICATION_ACTIVATION_CUTOFF: "2026-10-17T00:00:00Z" }, now), { eligible: false, queued: 0 });
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

    const env = { DB: db as never, NOTIFICATION_ACTIVATION_CUTOFF: cutoff };
    assert.deepEqual(await prepareNotificationQueue(env, now), { eligible: true, queued: 2 });
    assert.deepEqual(await prepareNotificationQueue(env, now), { eligible: true, queued: 0 }, "repeated queueing is idempotent");
    const rows = await db.prepare("SELECT u.telegram_id, e.event_type, p.relevance FROM notifications n JOIN users u ON u.telegram_id=n.telegram_id JOIN publication_events e ON e.id=n.event_id JOIN publications p ON p.id=e.publication_id ORDER BY u.telegram_id")
      .all<{ telegram_id: number; event_type: string; relevance: string }>();
    assert.deepEqual(rows.results.map((row) => ({ ...row })), [
      { telegram_id: 1, event_type: "TITLE_CHANGED", relevance: "DIRECT" },
      { telegram_id: 2, event_type: "TITLE_CHANGED", relevance: "POTENTIAL" },
    ]);
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
    assert.equal(calls, 0);

    const result = await deliverPendingNotifications({
      DB: db as never, NOTIFICATION_DELIVERY_ENABLED: "true", NOTIFICATION_ACTIVATION_CUTOFF: cutoff,
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
    const env = { DB: db as never, NOTIFICATION_DELIVERY_ENABLED: "true", NOTIFICATION_ACTIVATION_CUTOFF: cutoff };
    assert.deepEqual(await deliverPendingNotifications(env, failing, now), { enabled: true, sent: 0, failed: 1 });
    assert.deepEqual(await deliverPendingNotifications(env, failing, now), { enabled: true, sent: 0, failed: 0 });
    assert.equal(calls, 1);
    const row = await db.prepare("SELECT status, attempts, last_error FROM notifications WHERE event_id = ?").bind(eventId).first<{ status: string; attempts: number; last_error: string }>();
    assert.deepEqual({ ...row }, { status: "FAILED", attempts: 1, last_error: "telegram_delivery_ambiguous_or_failed" });
  } finally { db.close(); }
});

test("concurrent dispatchers claim a notification once; invalid URLs are never sent", async () => {
  const db = await fixture();
  try {
    const eventId = await addEvent(db, { id: "concurrent" });
    await db.prepare("INSERT INTO notifications (telegram_id, event_id, status, created_at) VALUES (1, ?, 'PENDING', ?)")
      .bind(eventId, now.toISOString()).run();
    let calls = 0;
    const mock = { async sendMessage() { calls += 1; } };
    const env = { DB: db as never, NOTIFICATION_DELIVERY_ENABLED: "true", NOTIFICATION_ACTIVATION_CUTOFF: cutoff };
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
