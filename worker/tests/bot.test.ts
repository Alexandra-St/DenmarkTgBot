import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "../src/index.ts";
import { handleTelegramUpdate } from "../src/bot/handlers/webhook.ts";
import { D1UserRepository, type Language, type UserPreferences, type UserRepository } from "../src/database/users.ts";
import { TelegramApi, type TelegramClient } from "../src/bot/telegram.ts";
import { languageKeyboard, settingsLanguageKeyboard } from "../src/bot/keyboards/index.ts";

class MemoryRepository implements UserRepository {
  readonly users = new Map<number, UserPreferences>();
  creates = 0;

  async find(id: number): Promise<UserPreferences | null> {
    return this.users.get(id) ?? null;
  }
  async create(id: number, language: Language): Promise<void> {
    if (this.users.has(id)) return;
    this.creates += 1;
    this.users.set(id, { telegram_id: id, language, notify_direct: 1, notify_potential: 0 });
  }
  async updateLanguage(id: number, language: Language): Promise<void> {
    const user = this.users.get(id);
    if (user) user.language = language;
  }
  async updateNotification(id: number, preference: "direct" | "potential", enabled: boolean): Promise<void> {
    const user = this.users.get(id);
    if (user) user[preference === "direct" ? "notify_direct" : "notify_potential"] = enabled ? 1 : 0;
  }
}

class MemoryTelegram implements TelegramClient {
  readonly sent: Array<{ chatId: number; text: string; keyboard?: unknown }> = [];
  readonly edited: Array<{ chatId: number; messageId: number; text: string; keyboard?: unknown }> = [];
  readonly answered: string[] = [];
  async sendMessage(chatId: number, text: string, keyboard?: unknown): Promise<void> {
    this.sent.push({ chatId, text, keyboard });
  }
  async editMessage(chatId: number, messageId: number, text: string, keyboard?: unknown): Promise<void> {
    this.edited.push({ chatId, messageId, text, keyboard });
  }
  async answerCallback(id: string): Promise<void> {
    this.answered.push(id);
  }
}

class MemoryD1 {
  readonly users = new Map<number, UserPreferences>();
  prepare(sql: string) {
    let values: unknown[] = [];
    const statement = {
      bind(...args: unknown[]) { values = args; return statement; },
      async first() {
        if (!sql.startsWith("SELECT")) throw new Error("Unexpected query");
        return thisDb.users.get(Number(values[0])) ?? null;
      },
      async run() {
        if (sql.startsWith("INSERT INTO users")) {
          const id = Number(values[0]);
          if (!thisDb.users.has(id)) {
            thisDb.users.set(id, {
              telegram_id: id,
              language: values[1] as Language,
              notify_direct: 1,
              notify_potential: 0,
            });
          }
        } else if (sql.startsWith("UPDATE users SET language")) {
          const user = thisDb.users.get(Number(values[2]));
          if (user) user.language = values[0] as Language;
        } else if (sql.includes("notify_direct = ?")) {
          const user = thisDb.users.get(Number(values[2]));
          if (user) user.notify_direct = Number(values[0]);
        } else if (sql.includes("notify_potential = ?")) {
          const user = thisDb.users.get(Number(values[2]));
          if (user) user.notify_potential = Number(values[0]);
        } else throw new Error("Unexpected query");
        return { success: true };
      },
    };
    const thisDb = this;
    return statement;
  }
}

const message = (id: number, text: string) => ({
  message: { message_id: 10, chat: { id }, from: { id }, text },
});
const callback = (id: number, data: string) => ({
  callback_query: {
    id: `callback-${id}-${data}`,
    from: { id },
    data,
    message: { message_id: 10, chat: { id } },
  },
});

test("new user sees language selection; both language selections persist with defaults", async () => {
  for (const [id, language, menuPrompt] of [
    [101, "uk", "Оберіть розділ"],
    [102, "ru", "Выберите раздел"],
  ] as const) {
    const repo = new MemoryRepository();
    const telegram = new MemoryTelegram();
    await handleTelegramUpdate(message(id, "/start"), repo, telegram);
    assert.match(telegram.sent[0].text, /Оберіть мову|Выберите язык/);
    const languageButtons = (telegram.sent[0].keyboard as {
      inline_keyboard: Array<Array<{ text: string; callback_data: string }>>;
    }).inline_keyboard;
    assert.deepEqual(languageButtons.slice(0, 2).map((row) => row[0]), [
      { text: "Українська", callback_data: "lang:uk" },
      { text: "Русский", callback_data: "lang:ru" },
    ]);
    assert.ok(languageButtons.flat().every((button) => !/[🇺🇦🇷🇺]/u.test(button.text)));
    assert.deepEqual(repo.users.size, 0);
    await handleTelegramUpdate(callback(id, `lang:${language}`), repo, telegram);
    assert.equal(repo.users.get(id)?.language, language);
    assert.equal(repo.users.get(id)?.notify_direct, 1);
    assert.equal(repo.users.get(id)?.notify_potential, 0);
    assert.match(telegram.edited.at(-1)!.text, new RegExp(menuPrompt));
    assert.equal(telegram.answered.length, 1);
  }
});

test("language selectors in the initial menu and settings use plain language names", () => {
  for (const markup of [languageKeyboard("uk"), settingsLanguageKeyboard("ru")]) {
    const labels = markup.inline_keyboard.slice(0, 2).map((row) => row[0].text);
    assert.deepEqual(labels, ["Українська", "Русский"]);
    assert.ok(markup.inline_keyboard.flat().every((button) => !/[🇺🇦🇷🇺]/u.test(button.text)));
  }
});

test("repeated start does not duplicate users and returning users retain language", async () => {
  const repo = new MemoryRepository();
  const telegram = new MemoryTelegram();
  await handleTelegramUpdate(callback(200, "lang:ru"), repo, telegram);
  await handleTelegramUpdate(callback(200, "lang:ru"), repo, telegram);
  await handleTelegramUpdate(message(200, "/start"), repo, telegram);
  await handleTelegramUpdate(message(200, "/start@DenmarkForUABot"), repo, telegram);
  assert.equal(repo.creates, 1);
  assert.equal(telegram.sent.length, 2);
  assert.match(telegram.sent[0].text, /Выберите раздел/);
});

test("explicit preference callbacks are idempotent and DIRECT/POTENTIAL stay independent", async () => {
  const repo = new MemoryRepository();
  const telegram = new MemoryTelegram();
  await handleTelegramUpdate(callback(300, "lang:uk"), repo, telegram);
  await handleTelegramUpdate(callback(300, "settings:notifications"), repo, telegram);
  const buttons = telegram.edited.at(-1)!.keyboard as {
    inline_keyboard: Array<Array<{ callback_data?: string }>>;
  };
  assert.equal(buttons.inline_keyboard[0][0].callback_data, "notify:direct:set:0");
  assert.equal(buttons.inline_keyboard[1][0].callback_data, "notify:potential:set:1");
  await handleTelegramUpdate(callback(300, "notify:potential:set:1"), repo, telegram);
  await handleTelegramUpdate(callback(300, "notify:potential:set:1"), repo, telegram);
  assert.deepEqual(
    [repo.users.get(300)?.notify_direct, repo.users.get(300)?.notify_potential],
    [1, 1],
  );
  await handleTelegramUpdate(callback(300, "notify:direct:set:0"), repo, telegram);
  await handleTelegramUpdate(callback(300, "notify:direct:set:0"), repo, telegram);
  assert.deepEqual(
    [repo.users.get(300)?.notify_direct, repo.users.get(300)?.notify_potential],
    [0, 1],
  );
  await handleTelegramUpdate(callback(300, "notify:direct"), repo, telegram);
  assert.deepEqual(
    [repo.users.get(300)?.notify_direct, repo.users.get(300)?.notify_potential],
    [0, 1],
  );
  await handleTelegramUpdate(callback(300, "lang:ru"), repo, telegram);
  assert.equal(repo.users.get(300)?.language, "ru");
  assert.match(telegram.edited.at(-1)!.text, /Настройки/);
});

test("About and unknown callbacks are handled in selected language", async () => {
  for (const [id, language, expected] of [
    [400, "ru", [/бесплатный информационный бот/, /Høringsportalen/, /Folketinget ещё не собраны/, /мониторинг и уведомления не включены/, /Telegram ID/, /не является юридической консультацией/]],
    [401, "uk", [/безкоштовний інформаційний бот/, /Høringsportalen/, /Folketinget ще не зібрані/, /моніторинг і сповіщення не ввімкнені/, /Telegram ID/, /не є юридичною консультацією/]],
  ] as const) {
    const repo = new MemoryRepository();
    const telegram = new MemoryTelegram();
    await handleTelegramUpdate(callback(id, `lang:${language}`), repo, telegram);
    await handleTelegramUpdate(callback(id, "menu:about"), repo, telegram);
    const about = telegram.edited.at(-1)!.text;
    for (const phrase of expected) assert.match(about, phrase);
    assert.ok(Array.from(about).length <= 4096, "About screen fits Telegram message limit");
    assert.deepEqual(
      [repo.users.get(id)?.notify_direct, repo.users.get(id)?.notify_potential],
      [1, 0],
      "About does not change notification preferences",
    );
    const count = telegram.edited.length;
    await handleTelegramUpdate(callback(id, "future:unknown"), repo, telegram);
    assert.equal(telegram.edited.length, count);
  }
});

test("D1 repository uses the existing user fields and independent defaults", async () => {
  const db = new MemoryD1();
  const repo = new D1UserRepository(db as never);
  await repo.create(500, "uk");
  assert.deepEqual(await repo.find(500), {
    telegram_id: 500,
    language: "uk",
    notify_direct: 1,
    notify_potential: 0,
  });
  await repo.updateLanguage(500, "ru");
  await repo.updateNotification(500, "direct", false);
  assert.deepEqual(await repo.find(500), {
    telegram_id: 500,
    language: "ru",
    notify_direct: 0,
    notify_potential: 0,
  });
});

test("webhook rejects wrong method, missing/invalid secret, malformed JSON, and unknown routes", async () => {
  const env = {
    DB: new MemoryD1() as never,
    TELEGRAM_WEBHOOK_SECRET: "test-secret",
    TELEGRAM_BOT_TOKEN: "test-token",
  };
  const call = (path: string, init?: RequestInit) => worker.fetch(new Request(`https://local${path}`, init), env);
  assert.equal((await call("/telegram/webhook", { method: "GET" })).status, 405);
  assert.equal((await worker.fetch(new Request("https://local/telegram/webhook", { method: "POST" }), { DB: env.DB } as never)).status, 503);
  assert.equal((await call("/telegram/webhook", { method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": "wrong" }, body: "{}" })).status, 401);
  assert.equal((await call("/telegram/webhook", { method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": "test-secret" }, body: "{" })).status, 400);
  assert.equal(
    (await worker.fetch(
      new Request("https://local/telegram/webhook", {
        method: "POST",
        headers: { "X-Telegram-Bot-Api-Secret-Token": "test-secret" },
        body: "{}",
      }),
      { DB: env.DB, TELEGRAM_WEBHOOK_SECRET: "test-secret" } as never,
    )).status,
    503,
  );
  assert.equal((await call("/other")).status, 404);
  assert.equal((await call("/health")).status, 200);
});

test("authenticated webhook persists both language choices, edits to main menu, and keeps /start idempotent", async () => {
  const db = new MemoryD1();
  const oldFetch = globalThis.fetch;
  const apiCalls: Array<{ url: string; payload: Record<string, unknown> }> = [];
  globalThis.fetch = async (input, init) => {
    apiCalls.push({
      url: String(input),
      payload: JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>,
    });
    return Response.json({ ok: true, result: true });
  };
  try {
    const env = {
      DB: db as never,
      TELEGRAM_WEBHOOK_SECRET: "test-secret",
      TELEGRAM_BOT_TOKEN: "test-token",
    };
    for (const [id, language, menuText] of [
      [600, "uk", "Оберіть розділ"],
      [601, "ru", "Выберите раздел"],
    ] as const) {
      const response = await worker.fetch(
        new Request("https://local/telegram/webhook", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": "test-secret",
          },
          body: JSON.stringify(callback(id, `lang:${language}`)),
        }),
        env,
      );
      assert.equal(response.status, 200);
      assert.equal(db.users.get(id)?.language, language);
      assert.match(String(apiCalls.at(-1)?.payload.text), new RegExp(menuText));
      assert.equal(apiCalls.at(-1)?.url.endsWith("/editMessageText"), true);

      for (let count = 0; count < 2; count += 1) {
        const startResponse = await worker.fetch(
          new Request("https://local/telegram/webhook", {
            method: "POST",
            headers: { "X-Telegram-Bot-Api-Secret-Token": "test-secret" },
            body: JSON.stringify(message(id, "/start")),
          }),
          env,
        );
        assert.equal(startResponse.status, 200);
        assert.match(String(apiCalls.at(-1)?.payload.text), new RegExp(menuText));
        assert.equal(apiCalls.at(-1)?.url.endsWith("/sendMessage"), true);
      }
    }
    assert.equal(db.users.size, 2);
    assert.equal(apiCalls.length, 8);
    assert.ok(apiCalls.every(({ url }) => url.startsWith("https://api.telegram.org/bottest-token/")));
  } finally {
    globalThis.fetch = oldFetch;
  }
});

test("Telegram unchanged-message and expired-ack errors are harmless; other errors remain failures", async () => {
  const oldFetch = globalThis.fetch;
  let description = "";
  globalThis.fetch = async () => Response.json(
    { ok: false, error_code: 400, description },
    { status: 400 },
  );
  try {
    const telegram = new TelegramApi("not-a-real-token");
    description = "Bad Request: message is not modified";
    await telegram.editMessage(1, 2, "same");
    description = "Bad Request: query is too old and response timeout expired or query ID is invalid";
    await telegram.answerCallback("expired-callback");
    description = "Bad Request: message can't be edited";
    await assert.rejects(() => telegram.editMessage(1, 2, "same"), /message can't be edited/);
  } finally {
    globalThis.fetch = oldFetch;
  }
});

test("unrelated Telegram API failures produce a retryable webhook error", async () => {
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    if (String(input).endsWith("/answerCallbackQuery")) return Response.json({ ok: true, result: true });
    return Response.json(
      { ok: false, error_code: 400, description: "Bad Request: chat not found" },
      { status: 400 },
    );
  };
  try {
    const db = new MemoryD1();
    db.users.set(700, { telegram_id: 700, language: "uk", notify_direct: 1, notify_potential: 0 });
    const response = await worker.fetch(
      new Request("https://local/telegram/webhook", {
        method: "POST",
        headers: { "X-Telegram-Bot-Api-Secret-Token": "test-secret" },
        body: JSON.stringify(callback(700, "menu:about")),
      }),
      { DB: db as never, TELEGRAM_WEBHOOK_SECRET: "test-secret", TELEGRAM_BOT_TOKEN: "not-a-real-token" },
    );
    assert.equal(response.status, 500);
    assert.doesNotMatch(await response.text(), /not-a-real-token/);
  } finally {
    globalThis.fetch = oldFetch;
  }
});

test("callback acknowledgement runs concurrently with D1 and UI work and emits redacted timing", async () => {
  const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
  const repository = new MemoryRepository();
  repository.users.set(701, { telegram_id: 701, language: "uk", notify_direct: 1, notify_potential: 0 });
  const telegram = new MemoryTelegram();
  let acknowledgementFinished = false;
  let editFinishedBeforeAcknowledgement = false;
  telegram.answerCallback = async (id: string) => {
    await delay(90);
    telegram.answered.push(id);
    acknowledgementFinished = true;
  };
  telegram.editMessage = async (chatId, messageId, text, keyboard) => {
    await delay(15);
    telegram.edited.push({ chatId, messageId, text, keyboard });
    editFinishedBeforeAcknowledgement = !acknowledgementFinished;
  };
  const slowRepository: UserRepository = {
    ...repository,
    find: async (id) => {
      await delay(30);
      return repository.find(id);
    },
    create: repository.create.bind(repository),
    updateLanguage: repository.updateLanguage.bind(repository),
    updateNotification: repository.updateNotification.bind(repository),
  };
  const originalInfo = console.info;
  const logLines: string[] = [];
  console.info = (value?: unknown) => { logLines.push(String(value)); };
  const baselineStarted = performance.now();
  await telegram.answerCallback("baseline-callback");
  await slowRepository.find(701);
  await telegram.editMessage(701, 10, "baseline", undefined);
  const sequentialBaselineMs = performance.now() - baselineStarted;
  acknowledgementFinished = false;
  editFinishedBeforeAcknowledgement = false;
  telegram.answered.length = 0;
  telegram.edited.length = 0;
  const started = performance.now();
  try {
    await handleTelegramUpdate(callback(701, "nav:main"), slowRepository, telegram);
  } finally {
    console.info = originalInfo;
  }
  const totalMs = performance.now() - started;
  assert.equal(editFinishedBeforeAcknowledgement, true);
  assert.equal(telegram.answered.length, 1);
  assert.equal(telegram.edited.length, 1);
  // The old sequential path would take roughly 90 + 30 + 15 ms.
  assert.ok(totalMs < sequentialBaselineMs - 25,
    `expected overlap: sequential=${sequentialBaselineMs.toFixed(1)} ms, concurrent=${totalMs.toFixed(1)} ms`);
  assert.equal(logLines.length, 1);
  const metric = JSON.parse(logLines[0]) as Record<string, unknown>;
  assert.equal(metric.event, "telegram_callback_timing");
  assert.equal(metric.route, "menu");
  for (const field of ["callback_ack_ms", "user_lookup_ms", "publication_lookup_ms", "telegram_edit_ms", "total_ms"]) {
    assert.equal(typeof metric[field], "number");
  }
  assert.equal(metric.success, "success");
  assert.doesNotMatch(logLines[0], /callback-701|nav:main|test-token|telegram_id|chat_id/);
});

test("Telegram API requests have bounded abort signals and transient write failures are not retried", async () => {
  const oldFetch = globalThis.fetch;
  let calls = 0;
  let boundedSignal = false;
  globalThis.fetch = async (_input, init) => {
    calls += 1;
    boundedSignal = init?.signal instanceof AbortSignal;
    return Response.json({ ok: false, error_code: 503, description: "temporary failure" }, { status: 503 });
  };
  try {
    const telegram = new TelegramApi("not-a-real-token");
    await assert.rejects(() => telegram.editMessage(1, 2, "content"), /temporary failure/);
    assert.equal(calls, 1);
    assert.equal(boundedSignal, true);
  } finally {
    globalThis.fetch = oldFetch;
  }
});

test("rapid repeated navigation callbacks edit the existing message without sending duplicates", async () => {
  const repository = new MemoryRepository();
  repository.users.set(702, { telegram_id: 702, language: "ru", notify_direct: 1, notify_potential: 0 });
  const telegram = new MemoryTelegram();
  await Promise.all([
    handleTelegramUpdate(callback(702, "nav:main"), repository, telegram),
    handleTelegramUpdate(callback(702, "nav:main"), repository, telegram),
  ]);
  assert.equal(telegram.answered.length, 2);
  assert.equal(telegram.edited.length, 2);
  assert.equal(telegram.sent.length, 0);
  assert.ok(telegram.edited.every(({ text }) => text.includes("Выберите раздел")));
});
