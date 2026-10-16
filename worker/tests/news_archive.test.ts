import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { DatabaseSync, type StatementSync } from "node:sqlite";
import { handleTelegramUpdate } from "../src/bot/handlers/webhook.ts";
import type { TelegramClient } from "../src/bot/telegram.ts";
import { D1PublicationRepository } from "../src/database/publications.ts";
import { D1UserRepository } from "../src/database/users.ts";

class CaptureTelegram implements TelegramClient {
  readonly edited: Array<{ text: string; keyboard?: unknown }> = [];
  async sendMessage(): Promise<void> {}
  async answerCallback(): Promise<void> {}
  async editMessage(_chatId: number, _messageId: number, text: string, keyboard?: unknown): Promise<void> {
    this.edited.push({ text, keyboard });
  }
}

class LocalD1Fixture {
  private readonly sqlite: DatabaseSync;
  constructor() { this.sqlite = new DatabaseSync(":memory:"); }
  exec(sql: string): void { this.sqlite.exec(sql); }
  prepare(sql: string) {
    let values: unknown[] = [];
    const statement = this.sqlite.prepare(sql);
    return {
      bind: (...args: unknown[]) => { values = args; return this.prepareBound(statement, () => values); },
      all: <T>() => ({ results: statement.all() as T[] }),
      first: <T>() => statement.get() as T | undefined ?? null,
      run: () => ({ success: true, meta: { changes: statement.run() } }),
    };
  }
  private prepareBound(statement: StatementSync, values: () => unknown[]) {
    return {
      all: <T>() => ({ results: statement.all(...values()) as T[] }),
      first: <T>() => (statement.get(...values()) as T | undefined) ?? null,
      run: () => ({ success: true, meta: statement.run(...values()) }),
    };
  }
  close(): void { this.sqlite.close(); }
}

const callback = (data: string) => ({
  callback_query: {
    id: `news-${data}`,
    from: { id: 9001 },
    data,
    message: { message_id: 1, chat: { id: 9001 } },
  },
});

test("News and Archive read only complete relevant records from local D1", async () => {
  const db = new LocalD1Fixture();
  try {
    for (const migration of ["0001_initial_schema.sql", "0002_ingestion.sql", "0003_ingestion_safety.sql", "0004_historical_baseline_gate.sql"]) {
      db.exec(await readFile(new URL(`../migrations/${migration}`, import.meta.url), "utf8"));
    }
    await new D1UserRepository(db).create(9001, "uk");
    const source = await db.prepare("SELECT id FROM sources WHERE code = 'hoeringsportalen'").first<{ id: number }>();
    assert.ok(source);
    for (let n = 1; n <= 12; n += 1) {
      const relevance = n === 12 ? "IRRELEVANT" : n === 11 ? "DIRECT" : n % 2 === 0 ? "DIRECT" : "POTENTIAL";
      const url = n === 1 ? "https://example.com/not-official" : `https://hoeringsportalen.dk/hearing/${n}`;
      await db.prepare(`INSERT INTO publications
        (source_id, external_id, external_url, original_title, relevance, current_status, source_updated_at,
         discovered_at, updated_at, source_complete)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
        .bind(source.id, `hp-${n}`, url, `Danish title ${n}`, relevance, "Open", `2026-09-${String(n).padStart(2, "0")}T10:00:00Z`, "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z", n === 10 ? 0 : 1)
        .run();
    }

    const repo = new D1PublicationRepository(db);
    const news = await repo.listNews(0);
    assert.equal(news.items.length, 5);
    assert.equal(news.hasNext, true);
    assert.ok(news.items.every((item) => item.relevance === "DIRECT" || item.relevance === "POTENTIAL"));
    const archiveDirect = await repo.listArchive("direct", 0);
    assert.ok(archiveDirect.items.every((item) => item.relevance === "DIRECT"));
    assert.equal(await repo.findById(10), null, "incomplete source record must not be exposed");
    assert.equal(await repo.findById(12), null, "IRRELEVANT record must not be exposed");

    const telegram = new CaptureTelegram();
    await handleTelegramUpdate(callback("menu:news"), new D1UserRepository(db), telegram, repo);
    assert.match(telegram.edited.at(-1)!.text, /Останні публікації/);
    assert.match(telegram.edited.at(-1)!.text, /Danish title/);
    const listMarkup = telegram.edited.at(-1)!.keyboard as { inline_keyboard: Array<Array<{ callback_data?: string }>> };
    assert.equal(listMarkup.inline_keyboard.flat().filter((button) => button.callback_data?.startsWith("pub:d:")).length, 5);
    await handleTelegramUpdate(callback("pub:d:1:news:all:0"), new D1UserRepository(db), telegram, repo);
    assert.match(telegram.edited.at(-1)!.text, /Оригінальна назва: Danish title 1/);
    const detailMarkup = telegram.edited.at(-1)!.keyboard as { inline_keyboard: Array<Array<{ url?: string }>> };
    assert.equal(detailMarkup.inline_keyboard.flat().some((button) => button.url), false, "untrusted external host must not become a URL button");
    await handleTelegramUpdate(callback("pub:d:2:news:all:0"), new D1UserRepository(db), telegram, repo);
    const validMarkup = telegram.edited.at(-1)!.keyboard as { inline_keyboard: Array<Array<{ url?: string }>> };
    assert.equal(validMarkup.inline_keyboard.flat().some((button) => button.url?.startsWith("https://hoeringsportalen.dk/")), true);

    const notifications = await db.prepare("SELECT COUNT(*) AS count FROM notifications").first<{ count: number }>();
    const events = await db.prepare("SELECT COUNT(*) AS count FROM publication_events").first<{ count: number }>();
    assert.equal(notifications?.count, 0);
    assert.equal(events?.count, 0);
    await handleTelegramUpdate(callback("pub:d:999:news:all:0"), new D1UserRepository(db), telegram, repo);
    assert.match(telegram.edited.at(-1)!.text, /більше не знайдено/);
    await handleTelegramUpdate(callback("pub:d:2:archive:potential:0"), new D1UserRepository(db), telegram, repo);
    assert.match(telegram.edited.at(-1)!.text, /більше не знайдено/);
    await handleTelegramUpdate(callback("archive:page:all:10000"), new D1UserRepository(db), telegram, repo);
    assert.match(telegram.edited.at(-1)!.text, /більше не знайдено/);
    await handleTelegramUpdate(callback(`pub:${"x".repeat(80)}`), new D1UserRepository(db), telegram, repo);
    assert.match(telegram.edited.at(-1)!.text, /більше не знайдено/);
  } finally {
    db.close();
  }
});
