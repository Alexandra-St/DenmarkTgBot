import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { DatabaseSync, type StatementSync } from "node:sqlite";
import { handleTelegramUpdate } from "../src/bot/handlers/webhook.ts";
import type { TelegramClient } from "../src/bot/telegram.ts";
import { D1PublicationRepository } from "../src/database/publications.ts";
import { D1UserRepository } from "../src/database/users.ts";
import { D1NavigationStateRepository } from "../src/database/navigation_state.ts";
import { validatedOfficialUrl } from "../src/bot/publications.ts";

class CaptureTelegram implements TelegramClient {
  readonly edited: Array<{ text: string; keyboard?: unknown }> = [];
  readonly sent: Array<{ text: string; keyboard?: unknown }> = [];
  async sendMessage(_chatId: number, text: string, keyboard?: unknown): Promise<void> { this.sent.push({ text, keyboard }); }
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

const callback = (data: string, userId = 9001) => ({
  callback_query: {
    id: `news-${data}`,
    from: { id: userId },
    data,
    message: { message_id: 1, chat: { id: userId } },
  },
});

test("News and Archive read only complete relevant records from local D1", async () => {
  const db = new LocalD1Fixture();
  try {
    for (const migration of ["0001_initial_schema.sql", "0002_ingestion.sql", "0003_ingestion_safety.sql", "0004_historical_baseline_gate.sql", "0005_notification_delivery_claim.sql", "0006_notification_canary_once.sql", "0008_source_deletions.sql", "0009_user_navigation_state.sql"]) {
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
    await db.prepare("UPDATE publications SET source_deleted=1 WHERE external_id='hp-2'").run();

    const repo = new D1PublicationRepository(db);
    const news = await repo.listNews(0);
    assert.equal(news.items.length, 5);
    assert.equal(news.hasNext, true);
    assert.ok(news.items.every((item) => item.relevance === "DIRECT" || item.relevance === "POTENTIAL"));
    assert.ok(news.items.every((item) => item.external_id !== "hp-2"), "confirmed source deletions must be hidden from News");
    const archiveDirect = await repo.listArchive("direct", 0);
    assert.ok(archiveDirect.items.every((item) => item.relevance === "DIRECT"));
    assert.ok(archiveDirect.items.every((item) => item.external_id !== "hp-2"), "confirmed source deletions must be hidden from Archive");
    assert.equal(await repo.findById(10), null, "incomplete source record must not be exposed");
    assert.equal(await repo.findById(12), null, "IRRELEVANT record must not be exposed");
    const deleted = await db.prepare("SELECT id FROM publications WHERE external_id='hp-2'").first<{ id: number }>();
    assert.ok(deleted);
    assert.equal(await repo.findById(deleted.id), null, "source-deleted detail must not be exposed");

    assert.equal(validatedOfficialUrl("hoeringsportalen", "https://hoeringsportalen.dk/hearing/1"), "https://hoeringsportalen.dk/hearing/1");
    assert.equal(validatedOfficialUrl("hoeringsportalen", "http://hoeringsportalen.dk/hearing/1"), null);
    assert.equal(validatedOfficialUrl("hoeringsportalen", "https://example.com/hearing/1"), null);
    assert.equal(validatedOfficialUrl("hoeringsportalen", "https://hoeringsportalen.dk.example.com/hearing/1"), null);
    assert.equal(validatedOfficialUrl("hoeringsportalen", "https://user@hoeringsportalen.dk/hearing/1"), null);
    assert.equal(validatedOfficialUrl("unknown", "https://unknown.example/"), null);

    const users = new D1UserRepository(db);
    const navigation = new D1NavigationStateRepository(db);
    const localized = [
      { id: 9001, language: "uk" as const, newsHeading: "Новини", archiveHeading: "Архів публікацій", empty: "У цій категорії поки немає публікацій", titleLabel: "Оригінальна назва" },
      { id: 9002, language: "ru" as const, newsHeading: "Новости", archiveHeading: "Архив публикаций", empty: "В этой категории пока нет публикаций", titleLabel: "Оригинальное название" },
    ];
    await users.create(9002, "ru");
    const navState = new D1NavigationStateRepository(db);
    const savedArchiveState = { screen: "archive" as const, parent_screen: "main" as const, publication_kind: "archive" as const, archive_filter: "potential" as const, page: 2 };
    await navState.set(9002, savedArchiveState);
    const restartedRepository = new D1NavigationStateRepository(db);
    assert.deepEqual({ ...await restartedRepository.get(9002)! }, savedArchiveState, "a new repository instance restores the saved navigation context");
    assert.equal(await restartedRepository.get(9001), null, "missing navigation state is not guessed");
    await db.prepare("UPDATE user_navigation_state SET screen = 'publication_detail', parent_screen = 'settings', publication_kind = 'news' WHERE telegram_id = ?")
      .bind(9002).run();
    assert.equal(await restartedRepository.get(9002), null, "malformed but SQL-valid state is rejected by the repository");
    for (const fixture of localized) {
      const telegram = new CaptureTelegram();
      const click = (data: string) => handleTelegramUpdate(callback(data, fixture.id), users, telegram, repo, undefined, new D1NavigationStateRepository(db));
      const press = (text: string) => handleTelegramUpdate({ message: { message_id: 2, chat: { id: fixture.id }, from: { id: fixture.id }, text } }, users, telegram, repo, undefined, new D1NavigationStateRepository(db));

      await click("menu:news");
      assert.match(telegram.edited.at(-1)!.text, new RegExp(fixture.newsHeading));
      assert.match(telegram.edited.at(-1)!.text, /не обов’язково нові або нещодавно опубліковані|не обязательно новые или недавно опубликованные/u);
      assert.match(telegram.edited.at(-1)!.text, /датою оновлення джерелом|дате обновления источником/u);
      assert.match(telegram.edited.at(-1)!.text, /доступні в боті офіційні публікації|доступные в боте официальные публикации/u);
      assert.match(telegram.edited.at(-1)!.text, /ті самі записи|одни и те же записи/u);
      assert.match(telegram.edited.at(-1)!.text, /моніторинг і сповіщення ще не ввімкнено|мониторинг и уведомления ещё не включены/u);
      assert.match(telegram.edited.at(-1)!.text, /Danish title/);
      assert.ok(Array.from(telegram.edited.at(-1)!.text).length <= 4096, "News fits Telegram message limit");
      let markup = telegram.edited.at(-1)!.keyboard as { inline_keyboard: Array<Array<{ text: string; callback_data?: string }>> };
      assert.equal(markup.inline_keyboard.flat().filter((button) => button.callback_data?.startsWith("pub:d:")).length, 5);
      assert.ok(markup.inline_keyboard.flat().every((button) => !button.callback_data || new TextEncoder().encode(button.callback_data).length <= 64));

      const nextNews = markup.inline_keyboard.flat().find((button) => button.text.includes("➡️"))!.callback_data!;
      await click(nextNews);
      const pageOneNews = telegram.edited.at(-1)!.text;
      assert.match(pageOneNews, /Danish title 5/);
      markup = telegram.edited.at(-1)!.keyboard as typeof markup;
      const detailCallback = markup.inline_keyboard.flat().find((button) => button.callback_data?.startsWith("pub:d:"))!.callback_data!;
      await click(detailCallback);
      assert.match(telegram.edited.at(-1)!.text, new RegExp(`${fixture.titleLabel}: Danish title`));
      assert.match(telegram.edited.at(-1)!.text, /а не юридична консультація|а не юридическая консультация/u);
      const detailMarkup = telegram.edited.at(-1)!.keyboard as { inline_keyboard: Array<Array<{ text: string; callback_data?: string; url?: string }>> };
      assert.ok(detailMarkup.inline_keyboard.flat().some((button) => button.url?.startsWith("https://hoeringsportalen.dk/")));
      assert.equal(detailMarkup.inline_keyboard.flat().some((button) => button.callback_data), false, "navigation is provided by Reply Keyboard");
      await press("⬅️ Назад");
      assert.equal(telegram.sent.at(-2)!.text, pageOneNews, "detail Back returns to the same News page after repository recreation");
      await click("pub:d:1:news:all:1");
      assert.equal((telegram.edited.at(-1)!.keyboard as { inline_keyboard: Array<Array<{ url?: string }>> }).inline_keyboard.flat().some((button) => button.url), false, "non-official URL is never exposed");
      await click("news:page:9999");
      assert.match(telegram.edited.at(-1)!.text, /Наразі немає публікацій для показу|Сейчас нет публикаций для показа/u);
      assert.match(telegram.edited.at(-1)!.text, /датою оновлення джерелом|дате обновления источником/u);
      assert.match(telegram.edited.at(-1)!.text, /моніторинг і сповіщення ще не ввімкнено|мониторинг и уведомления ещё не включены/u);
      await click("news:page:1");
      markup = telegram.edited.at(-1)!.keyboard as typeof markup;
      await press("⬅️ Назад");
      assert.match(telegram.sent.at(-1)!.text, /Оберіть розділ|Выберите раздел/u);

      await click("menu:archive");
      assert.match(telegram.edited.at(-1)!.text, new RegExp(fixture.archiveHeading));
      assert.match(telegram.edited.at(-1)!.text, /Усі публікації, що стосуються теми|Все публикации по теме/u);
      assert.match(telegram.edited.at(-1)!.text, /🟢 DIRECT/u);
      assert.match(telegram.edited.at(-1)!.text, /🟡 POTENTIAL/u);
      assert.match(telegram.edited.at(-1)!.text, /ті самі записи|одни и те же записи/u);
      assert.ok(Array.from(telegram.edited.at(-1)!.text).length <= 4096, "Archive fits Telegram message limit");
      markup = telegram.edited.at(-1)!.keyboard as typeof markup;
      const filterButton = (name: string) => markup.inline_keyboard.flat().find((button) => button.callback_data === `archive:filter:${name}:0`)!.callback_data!;
      assert.ok(markup.inline_keyboard.flat().some((button) => button.text === "🟢 DIRECT"));
      assert.ok(markup.inline_keyboard.flat().some((button) => button.text === "🟡 POTENTIAL"));
      await click(filterButton("direct"));
      let publicationRows = telegram.edited.at(-1)!.text.split("\n").filter((line) => /^[🟢🟡] Danish title/u.test(line));
      assert.ok(publicationRows.length > 0 && publicationRows.every((line) => line.startsWith("🟢")), "DIRECT filter excludes POTENTIAL entries");
      markup = telegram.edited.at(-1)!.keyboard as typeof markup;
      assert.ok(markup.inline_keyboard.flat().some((button) => button.callback_data === "archive:filter:potential:0"));
      await click("archive:filter:potential:0");
      publicationRows = telegram.edited.at(-1)!.text.split("\n").filter((line) => /^[🟢🟡] Danish title/u.test(line));
      assert.ok(publicationRows.length > 0 && publicationRows.every((line) => line.startsWith("🟡")), "POTENTIAL filter excludes DIRECT entries");
      await click("archive:filter:all:0");
      markup = telegram.edited.at(-1)!.keyboard as typeof markup;
      await click(markup.inline_keyboard.flat().find((button) => button.text.includes("➡️"))!.callback_data!);
      const pageOneArchive = telegram.edited.at(-1)!.text;
      assert.match(pageOneArchive, /Danish title 5/);
      markup = telegram.edited.at(-1)!.keyboard as typeof markup;
      const archiveDetail = markup.inline_keyboard.flat().find((button) => button.callback_data?.startsWith("pub:d:"))!.callback_data!;
      await click(archiveDetail);
      await press("⬅️ Назад");
      assert.equal(telegram.sent.at(-2)!.text, pageOneArchive, "detail Back returns to the same Archive filter and page");
      markup = telegram.edited.at(-1)!.keyboard as typeof markup;
      await press("⬅️ Назад");
      assert.match(telegram.sent.at(-1)!.text, /Оберіть розділ|Выберите раздел/u);

      await click("archive:page:potential:9999");
      assert.match(telegram.edited.at(-1)!.text, new RegExp(fixture.empty));
      assert.match(telegram.edited.at(-1)!.text, /🟢 DIRECT/u);
      assert.match(telegram.edited.at(-1)!.text, /🟡 POTENTIAL/u);
      assert.match(telegram.edited.at(-1)!.text, /моніторинг і сповіщення ще не ввімкнено|мониторинг и уведомления ещё не включены/u);
      await click("pub:d:999:news:all:0");
      assert.match(telegram.edited.at(-1)!.text, /більше не знайдено|больше не найдена/u);
      await click("pub:d:2:archive:potential:0");
      assert.match(telegram.edited.at(-1)!.text, /більше не знайдено|больше не найдена/u);
      await click("archive:page:all:10000");
      assert.match(telegram.edited.at(-1)!.text, /більше не знайдено|больше не найдена/u);
      await click(`pub:${"x".repeat(80)}`);
      assert.match(telegram.edited.at(-1)!.text, /більше не знайдено|больше не найдена/u);
    }

    const notifications = await db.prepare("SELECT COUNT(*) AS count FROM notifications").first<{ count: number }>();
    const events = await db.prepare("SELECT COUNT(*) AS count FROM publication_events").first<{ count: number }>();
    assert.equal(notifications?.count, 0);
    assert.equal(events?.count, 0);
  } finally {
    db.close();
  }
});
