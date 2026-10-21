import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "../src/index.ts";
import { handleTelegramUpdate } from "../src/bot/handlers/webhook.ts";
import { D1UserRepository, type Language, type UserPreferences, type UserRepository } from "../src/database/users.ts";
import { t } from "../src/localization/index.ts";
import { type NavigationStateRepository, type UserNavigationState } from "../src/database/navigation_state.ts";
import type { SourceStatusRepository } from "../src/database/source_status.ts";
import { TelegramApi, type TelegramClient } from "../src/bot/telegram.ts";
import { detailReplyKeyboard, languageKeyboard, mainKeyboard, notificationKeyboard, notificationMainMenuKeyboard, replyKeyboardRemoval, sectionNavigationKeyboard, settingsKeyboard, settingsLanguageKeyboard } from "../src/bot/keyboards/index.ts";

class MemoryRepository implements UserRepository {
  readonly users = new Map<number, UserPreferences>();
  creates = 0;

  async find(id: number): Promise<UserPreferences | null> {
    return this.users.get(id) ?? null;
  }
  async create(id: number, language: Language): Promise<boolean> {
    if (this.users.has(id)) return false;
    this.creates += 1;
    this.users.set(id, { telegram_id: id, language, notify_direct: 1, notify_potential: 0 });
    return true;
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

class MemoryNavigation implements NavigationStateRepository {
  readonly states: Map<number, UserNavigationState>;
  readonly screenIds = new Map<number, number>();
  constructor(states = new Map<number, UserNavigationState>(), screenIds = new Map<number, number>()) { this.states = states; this.screenIds = screenIds; }
  async get(id: number): Promise<UserNavigationState | null> { return this.states.get(id) ?? null; }
  async set(id: number, state: UserNavigationState): Promise<void> { this.states.set(id, { ...state }); }
  async getScreenMessageId(id: number): Promise<number | null> { return this.screenIds.get(id) ?? null; }
  async replaceScreen(id: number, expected: number | null, state: UserNavigationState, next: number): Promise<boolean> {
    if ((this.screenIds.get(id) ?? null) !== expected) return false;
    this.states.set(id, { ...state });
    this.screenIds.set(id, next);
    return true;
  }
}

class MemorySourceStatus implements SourceStatusRepository {
  private readonly timestamp: string | null;
  constructor(timestamp: string | null) { this.timestamp = timestamp; }
  async lastSuccessfulHoeringsportalenRun(): Promise<string | null> { return this.timestamp; }
}

class MemoryTelegram implements TelegramClient {
  readonly sent: Array<{ chatId: number; text: string; keyboard?: unknown }> = [];
  readonly edited: Array<{ chatId: number; messageId: number; text: string; keyboard?: unknown }> = [];
  readonly answered: string[] = [];
  readonly deleted: Array<{ chatId: number; messageId: number }> = [];
  readonly events: Array<{ kind: "send" | "edit"; text: string; keyboard?: unknown }> = [];
  private nextMessageId = 100;
  failDelete = false;
  failScreenSend = false;
  async sendMessage(chatId: number, text: string, keyboard?: unknown): Promise<void> {
    this.sent.push({ chatId, text, keyboard });
    this.events.push({ kind: "send", text, keyboard });
  }
  async sendScreenMessage(chatId: number, text: string, keyboard?: unknown): Promise<{ message_id: number }> {
    if (this.failScreenSend) throw new Error("send failed");
    await this.sendMessage(chatId, text, keyboard);
    return { message_id: this.nextMessageId++ };
  }
  async editMessage(chatId: number, messageId: number, text: string, keyboard?: unknown): Promise<void> {
    this.edited.push({ chatId, messageId, text, keyboard });
    this.events.push({ kind: "edit", text, keyboard });
  }
  async answerCallback(id: string): Promise<void> {
    this.answered.push(id);
  }
  async deleteMessage(chatId: number, messageId: number): Promise<void> {
    this.deleted.push({ chatId, messageId });
    if (this.failDelete) throw new Error("delete failed");
  }
}

class MemoryD1 {
  readonly users = new Map<number, UserPreferences>();
  readonly navigation = new Map<number, Record<string, unknown>>();
  prepare(sql: string) {
    let values: unknown[] = [];
    const statement = {
      bind(...args: unknown[]) { values = args; return statement; },
      async first() {
        if (sql.includes("FROM user_navigation_state")) return thisDb.navigation.get(Number(values[0])) ?? null;
        if (sql.startsWith("SELECT")) return thisDb.users.get(Number(values[0])) ?? null;
        if (sql.startsWith("INSERT INTO users")) {
          const id = Number(values[0]);
          if (thisDb.users.has(id)) return null;
          thisDb.users.set(id, {
            telegram_id: id,
            language: values[1] as Language,
            notify_direct: 1,
            notify_potential: 0,
          });
          return { telegram_id: id };
        }
        throw new Error("Unexpected query");
      },
      async run() {
        if (sql.includes("INSERT INTO user_navigation_state")) {
          const id = Number(values[0]);
          const current = thisDb.navigation.get(id);
          const replacing = sql.includes("last_screen_message_id IS ?");
          if (replacing && (current?.last_screen_message_id ?? null) !== (values[8] ?? null)) return { success: true, meta: { changes: 0 } };
          thisDb.navigation.set(id, {
            screen: values[1], parent_screen: values[2], publication_kind: values[3],
            archive_filter: values[4], page: values[5],
            last_screen_message_id: replacing ? values[7] : current?.last_screen_message_id ?? null,
          });
          return { success: true, meta: { changes: 1 } };
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

const message = (id: number, text: string, messageId = 10, type: "private" | "group" = "private") => ({
  message: { message_id: messageId, chat: { id, type }, from: { id }, text },
});
const callback = (id: number, data: string) => ({
  callback_query: {
    id: `callback-${id}-${data}`,
    from: { id },
    data,
    message: { message_id: 10, chat: { id, type: "private" } },
  },
});

test("first language selection sends localized first-time welcome and then main menu", async () => {
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
    const welcome = telegram.sent.at(-2)!.text;
    assert.match(welcome, language === "uk" ? /Вітаємо в DenmarkBot/u : /Добро пожаловать в DenmarkBot/u);
    assert.match(welcome, language === "uk" ? /Høringsportalen/u : /Høringsportalen/u);
    assert.match(welcome, language === "uk" ? /Folketinget ще розробляється/u : /Folketinget ещё разрабатывается/u);
    assert.match(welcome, language === "uk" ? /Сповіщення поки тестуються й не надсилаються/u : /Уведомления пока тестируются и не отправляются/u);
    assert.ok(welcome.length < 4096);
    assert.equal(telegram.edited.at(-1)!.text, language === "uk" ? "Мову збережено." : "Язык сохранён.");
    assert.deepEqual(telegram.edited.at(-1)!.keyboard, { inline_keyboard: [] });
    assert.match(telegram.sent.at(-1)!.text, new RegExp(menuPrompt));
    assert.deepEqual(telegram.sent.at(-1)!.keyboard, mainKeyboard(language));
    const flow = telegram.events.slice(-3);
    assert.deepEqual(flow.map(({ kind }) => kind), ["edit", "send", "send"]);
    assert.match(flow[0].text, /Мову збережено|Язык сохранён/u);
    assert.match(flow[1].text, /Добро пожаловать в DenmarkBot|Вітаємо в DenmarkBot/u);
    assert.match(flow[2].text, new RegExp(menuPrompt));
    assert.equal(telegram.answered.length, 1);
  }
});

test("initial language selection remains inline and Settings language choices use localized Reply Keyboard", () => {
  const onboardingMarkup = languageKeyboard("uk");
  {
    const labels = onboardingMarkup.inline_keyboard.slice(0, 2).map((row) => row[0].text);
    assert.deepEqual(labels, ["Українська", "Русский"]);
    assert.ok(onboardingMarkup.inline_keyboard.flat().every((button) => !/[🇺🇦🇷🇺]/u.test(button.text)));
  }
  const markup = settingsLanguageKeyboard("ru");
  assert.deepEqual(markup.keyboard[0], ["Українська", "Русский"]);
  assert.deepEqual(markup.keyboard[1], ["⬅️ Назад", "🏠 Главное меню"]);
  assert.equal(markup.is_persistent, true);
  assert.equal(markup.resize_keyboard, true);
});

test("main menu is a persistent localized Reply Keyboard with four buttons in two rows", () => {
  assert.deepEqual(mainKeyboard("uk").keyboard, [
    ["📰 Новини", "📚 Архів"], ["⚙️ Налаштування", "ℹ️ Про бота"],
  ]);
  assert.deepEqual(mainKeyboard("ru").keyboard, [
    ["📰 Новости", "📚 Архив"], ["⚙️ Настройки", "ℹ️ О боте"],
  ]);
  assert.equal(mainKeyboard("uk").is_persistent, true);
  assert.equal(mainKeyboard("uk").resize_keyboard, true);
});

test("publication controls stay inline while section navigation uses contextual Reply keyboards", async () => {
  for (const [id, language, labels, headings] of [
    [290, "uk", ["menu:news", "menu:archive", "menu:settings", "menu:about"], ["📰 Новини", "📚 Архів публікацій", "⚙️ Налаштування"]],
    [291, "ru", ["menu:news", "menu:archive", "menu:settings", "menu:about"], ["📰 Новости", "📚 Архив публикаций", "⚙️ Настройки"]],
  ] as const) {
    const repo = new MemoryRepository();
    const telegram = new MemoryTelegram();
    repo.users.set(id, { telegram_id: id, language, notify_direct: 1, notify_potential: 0 });
    const publications = {
      async listNews() { return { items: [], hasNext: false }; },
      async listArchive() { return { items: [], hasNext: false }; },
      async findById() { return null; },
    };
    const status = new MemorySourceStatus("2026-10-20T12:00:00.000Z");
    const navigation = new MemoryNavigation();
    for (const label of labels) await handleTelegramUpdate(callback(id, label), repo, telegram, publications, status, navigation);
    assert.equal(telegram.edited.length, 2, "News and Archive inline controls are attached after sending the Reply keyboard");
    assert.equal(telegram.sent.length, 4, "each section screen is sent with its contextual Reply keyboard");
    assert.match(telegram.edited[0].text, new RegExp(headings[0]));
    assert.match(telegram.edited[1].text, new RegExp(headings[1]));
    assert.match(telegram.sent[2].text, new RegExp(headings[2]));
    assert.match(telegram.sent[3].text, language === "uk" ? /Статус офіційних джерел/u : /Статус официальных источников/u);
    assert.deepEqual(sectionNavigationKeyboard(language).keyboard, [[language === "uk" ? "⬅️ Назад" : "⬅️ Назад"]]);
    assert.equal(navigation.states.get(id)?.screen, "about");
    await handleTelegramUpdate(callback(id, "nav:main"), repo, telegram, publications, status, navigation);
    assert.deepEqual(telegram.sent.at(-1)?.keyboard, mainKeyboard(language));
  }
});

test("Reply Keyboard settings navigation survives restart and updates preferences/language", async () => {
  const repo = new MemoryRepository();
  const telegram = new MemoryTelegram();
  const persisted = new Map<number, UserNavigationState>();
  const screenIds = new Map<number, number>();
  const firstWorkerNavigation = new MemoryNavigation(persisted, screenIds);
  const restartedWorkerNavigation = new MemoryNavigation(persisted, screenIds);
  repo.users.set(292, { telegram_id: 292, language: "ru", notify_direct: 1, notify_potential: 0 });

  await handleTelegramUpdate(message(292, "/start"), repo, telegram, undefined, undefined, firstWorkerNavigation);
  await handleTelegramUpdate(message(292, "⚙️ Настройки", 11), repo, telegram, undefined, undefined, firstWorkerNavigation);
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, settingsKeyboard("ru"));
  await handleTelegramUpdate(message(292, "🔔 Уведомления", 12), repo, telegram, undefined, undefined, firstWorkerNavigation);
  assert.equal(persisted.get(292)?.screen, "settings_notifications");
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, notificationKeyboard("ru", true, false));
  await handleTelegramUpdate(message(292, "✅ DIRECT", 13), repo, telegram, undefined, undefined, restartedWorkerNavigation);
  assert.equal(repo.users.get(292)?.notify_direct, 0);
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, notificationKeyboard("ru", false, false));
  await handleTelegramUpdate(message(292, "⬜ POTENTIAL", 14), repo, telegram, undefined, undefined, restartedWorkerNavigation);
  assert.equal(repo.users.get(292)?.notify_potential, 1);
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, notificationKeyboard("ru", false, true));

  await handleTelegramUpdate(message(292, "⬅️ Назад", 15), repo, telegram, undefined, undefined, restartedWorkerNavigation);
  assert.equal(persisted.get(292)?.screen, "settings");
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, settingsKeyboard("ru"));
  await handleTelegramUpdate(message(292, "🌐 Язык", 16), repo, telegram, undefined, undefined, restartedWorkerNavigation);
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, settingsLanguageKeyboard("ru"));
  await handleTelegramUpdate(message(292, "Українська", 17), repo, telegram, undefined, undefined, restartedWorkerNavigation);
  assert.equal(repo.users.get(292)?.language, "uk");
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, settingsKeyboard("uk"));
  await handleTelegramUpdate(message(292, "⬅️ Назад", 18), repo, telegram, undefined, undefined, restartedWorkerNavigation);
  assert.equal(persisted.get(292)?.screen, "main");
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, mainKeyboard("uk"));
  assert.equal(screenIds.get(292), telegram.sent.length + 99);
});

test("missing or invalid navigation state safely sends the main menu", async () => {
  const repo = new MemoryRepository();
  const telegram = new MemoryTelegram();
  const navigation = new MemoryNavigation();
  repo.users.set(293, { telegram_id: 293, language: "uk", notify_direct: 1, notify_potential: 0 });
  navigation.states.set(293, { screen: "publication_detail", parent_screen: "settings", publication_kind: "news", archive_filter: null, page: 0 });
  await handleTelegramUpdate(callback(293, "nav:back"), repo, telegram, undefined, undefined, navigation);
  assert.equal(navigation.states.get(293)?.screen, "main");
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, mainKeyboard("uk"));
});

test("repeated start does not duplicate users and returning users retain language", async () => {
  const repo = new MemoryRepository();
  const telegram = new MemoryTelegram();
  await handleTelegramUpdate(callback(200, "lang:ru"), repo, telegram);
  await handleTelegramUpdate(callback(200, "lang:ru"), repo, telegram);
  await handleTelegramUpdate(message(200, "/start"), repo, telegram);
  await handleTelegramUpdate(message(200, "/start@DenmarkForUABot"), repo, telegram);
  assert.equal(repo.creates, 1);
  assert.equal(telegram.sent.length, 5);
  assert.equal(telegram.sent.filter(({ text }) => text.includes("Добро пожаловать")).length, 1);
  assert.equal(telegram.sent.filter(({ keyboard }) => JSON.stringify(keyboard) === JSON.stringify(replyKeyboardRemoval())).length, 0);
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, mainKeyboard("ru"));
  assert.match(telegram.sent.at(-1)!.text, /Выберите раздел/);
});

test("existing users and Settings language changes never receive first-time welcome", async () => {
  const repo = new MemoryRepository();
  const telegram = new MemoryTelegram();
  repo.users.set(249, { telegram_id: 249, language: "ru", notify_direct: 0, notify_potential: 1 });
  await handleTelegramUpdate(message(249, "/start"), repo, telegram);
  await handleTelegramUpdate(callback(249, "lang:uk"), repo, telegram);
  assert.equal(telegram.sent.some(({ text }) => text.includes("Добро пожаловать в DenmarkBot") || text.includes("Вітаємо в DenmarkBot")), false);
  assert.deepEqual(repo.users.get(249), { telegram_id: 249, language: "uk", notify_direct: 0, notify_potential: 1 });
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, settingsKeyboard("uk"));

  await handleTelegramUpdate(callback(250, "lang:uk"), repo, telegram);
  const before = { ...repo.users.get(250)! };
  await handleTelegramUpdate(callback(250, "lang:ru"), repo, telegram);
  assert.equal(telegram.sent.filter(({ text }) => text.includes("Вітаємо в DenmarkBot")).length, 1);
  assert.equal(repo.users.get(250)?.language, "ru");
  assert.equal(repo.users.get(250)?.notify_direct, before.notify_direct);
  assert.equal(repo.users.get(250)?.notify_potential, before.notify_potential);
  assert.match(telegram.sent.at(-1)!.text, /⚙️ Настройки/);
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, settingsKeyboard("ru"));
});

test("concurrent duplicate first language callbacks only send one welcome", async () => {
  const repo = new MemoryRepository();
  const telegram = new MemoryTelegram();
  await Promise.all([
    handleTelegramUpdate(callback(251, "lang:ru"), repo, telegram),
    handleTelegramUpdate(callback(251, "lang:ru"), repo, telegram),
  ]);
  assert.equal(repo.creates, 1);
  assert.equal(telegram.sent.filter(({ text }) => text.includes("Добро пожаловать в DenmarkBot")).length, 1);
  assert.equal(telegram.sent.filter(({ text }) => /Выберите раздел/u.test(text)).length, 2);
});

test("/help shows the same localized instructions with the main menu for existing users", async () => {
  for (const [id, language, expected] of [
    [252, "uk", /Вітаємо в DenmarkBot/u],
    [253, "ru", /Добро пожаловать в DenmarkBot/u],
  ] as const) {
    const repo = new MemoryRepository();
    const telegram = new MemoryTelegram();
    repo.users.set(id, { telegram_id: id, language, notify_direct: 0, notify_potential: 1 });
    await handleTelegramUpdate(message(id, "/help@DenmarkForUABot"), repo, telegram);
    assert.equal(telegram.sent.length, 1);
    assert.match(telegram.sent[0].text, expected);
    assert.ok(telegram.sent[0].text.startsWith(t(language, "firstTimeWelcome")));
    assert.deepEqual(telegram.sent[0].keyboard, mainKeyboard(language));
    assert.equal(repo.users.get(id)?.notify_direct, 0);
    assert.equal(repo.users.get(id)?.notify_potential, 1);
  }
});

test("explicit preference callbacks are idempotent and DIRECT/POTENTIAL stay independent", async () => {
  const repo = new MemoryRepository();
  const telegram = new MemoryTelegram();
  await handleTelegramUpdate(callback(300, "lang:uk"), repo, telegram);
  await handleTelegramUpdate(callback(300, "settings:notifications"), repo, telegram);
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, notificationKeyboard("uk", true, false));
  const ukNotificationKeyboard = telegram.sent.at(-1)!.keyboard as { keyboard: string[][] };
  assert.deepEqual(ukNotificationKeyboard.keyboard, [["✅ DIRECT"], ["⬜ POTENTIAL"], ["⬅️ Назад", "🏠 Головне меню"]]);
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
  assert.match(telegram.sent.at(-1)!.text, /Настройки/);
});

test("publication detail exposes contextual Reply Back/Home without duplicate inline navigation", () => {
  assert.deepEqual(detailReplyKeyboard("uk").keyboard, [["⬅️ Назад", "🏠 Головне меню"]]);
  assert.deepEqual(detailReplyKeyboard("ru").keyboard, [["⬅️ Назад", "🏠 Главное меню"]]);
  assert.deepEqual(sectionNavigationKeyboard("uk").keyboard, [["⬅️ Назад"]]);
});

test("publication detail Reply Home returns to the root menu", async () => {
  const repo = new MemoryRepository();
  const telegram = new MemoryTelegram();
  const navigation = new MemoryNavigation(new Map([[406, {
    screen: "publication_detail", parent_screen: "archive", publication_kind: "archive", archive_filter: "direct", page: 2,
  }]]));
  repo.users.set(406, { telegram_id: 406, language: "ru", notify_direct: 1, notify_potential: 1 });
  await handleTelegramUpdate(message(406, "🏠 Главное меню", 61), repo, telegram, undefined, undefined, navigation);
  assert.equal(navigation.states.get(406)?.screen, "main");
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, mainKeyboard("ru"));
});

test("only recognized private Reply Keyboard presses are deleted; ordinary text and group messages remain", async () => {
  const repo = new MemoryRepository();
  const telegram = new MemoryTelegram();
  const navigation = new MemoryNavigation();
  repo.users.set(403, { telegram_id: 403, language: "ru", notify_direct: 0, notify_potential: 1 });
  await navigation.set(403, { screen: "main", parent_screen: null, publication_kind: null, archive_filter: null, page: 0 });
  const publications = {
    async listNews() { return { items: [], hasNext: false }; },
    async listArchive() { return { items: [], hasNext: false }; },
    async findById() { return null; },
  };
  await handleTelegramUpdate(message(403, "просто текст", 30), repo, telegram, publications, undefined, navigation);
  assert.equal(telegram.sent.length, 0);
  assert.deepEqual(telegram.deleted, []);
  await handleTelegramUpdate(message(403, "📰 Новости", 31, "group"), repo, telegram, publications, undefined, navigation);
  assert.equal(telegram.deleted.length, 0, "group messages are never deleted or routed as keyboard presses");
  await handleTelegramUpdate(message(403, "📰 Новости", 32), repo, telegram, publications, undefined, navigation);
  assert.equal(telegram.deleted[0].messageId, 32, "recognized private button press is deleted");
  assert.equal(navigation.states.get(403)?.screen, "news");
  assert.ok((telegram.sent.at(-1)?.keyboard as { inline_keyboard: unknown }).inline_keyboard,
    "the new screen keeps its inline publication controls alongside the Reply Keyboard");
  assert.equal(navigation.screenIds.get(403), 100, "the successfully sent current screen ID is persisted");
  // The key text is inherently indistinguishable from a user typing the exact same label in this context.
  assert.equal(telegram.deleted.length, 1);
  await handleTelegramUpdate(message(403, "/start"), repo, telegram, undefined, undefined, navigation);
  assert.deepEqual(telegram.sent.at(-1)?.keyboard, mainKeyboard("ru"));
  assert.equal(telegram.deleted.includes(10), false, "/start is not treated as a button press");
});

test("notification Home sends a new main menu and leaves the notification message unchanged", async () => {
  const repo = new MemoryRepository();
  const telegram = new MemoryTelegram();
  repo.users.set(404, { telegram_id: 404, language: "uk", notify_direct: 1, notify_potential: 0 });
  const notificationKeyboardMarkup = notificationMainMenuKeyboard("uk");
  const original = { message_id: 777, chat: { id: 404, type: "private" as const } };
  await handleTelegramUpdate({ callback_query: { id: "notification-home", from: { id: 404 }, data: "notification:main", message: original } }, repo, telegram);
  assert.equal(telegram.edited.length, 0, "notification is not edited");
  assert.equal(telegram.deleted.includes(777), false, "notification message is never deleted");
  assert.equal(telegram.sent.length, 1);
  assert.match(telegram.sent[0].text, /Оберіть розділ/u);
  assert.deepEqual(telegram.sent[0].keyboard, mainKeyboard("uk"));
  assert.deepEqual(notificationKeyboardMarkup.inline_keyboard.flat().map((button) => button.callback_data), ["notification:main"]);
});

test("failed button and old-screen deletions do not interrupt navigation or retry", async () => {
  const repo = new MemoryRepository();
  const telegram = new MemoryTelegram();
  telegram.failDelete = true;
  const navigation = new MemoryNavigation();
  repo.users.set(405, { telegram_id: 405, language: "ru", notify_direct: 1, notify_potential: 0 });
  await navigation.set(405, { screen: "main", parent_screen: null, publication_kind: null, archive_filter: null, page: 0 });
  assert.equal(await navigation.replaceScreen(405, null, {
    screen: "main", parent_screen: null, publication_kind: null, archive_filter: null, page: 0,
  }, 90), true);
  const publications = {
    async listNews() { return { items: [], hasNext: false }; },
    async listArchive() { return { items: [], hasNext: false }; },
    async findById() { return null; },
  };

  await handleTelegramUpdate(message(405, "📰 Новости", 55), repo, telegram, publications, undefined, navigation);

  assert.equal(navigation.states.get(405)?.screen, "news");
  assert.equal(navigation.screenIds.get(405), 100, "new screen ID is persisted despite Telegram deletion errors");
  assert.deepEqual(telegram.deleted.map(({ messageId }) => messageId), [55, 90],
    "one attempt is made for the recognized button and the exact persisted prior screen only");
});

test("failed screen send never replaces the saved ID or deletes the prior screen", async () => {
  const repo = new MemoryRepository();
  const telegram = new MemoryTelegram();
  telegram.failScreenSend = true;
  const navigation = new MemoryNavigation();
  repo.users.set(407, { telegram_id: 407, language: "uk", notify_direct: 1, notify_potential: 0 });
  await navigation.set(407, { screen: "main", parent_screen: null, publication_kind: null, archive_filter: null, page: 0 });
  assert.equal(await navigation.replaceScreen(407, null, {
    screen: "main", parent_screen: null, publication_kind: null, archive_filter: null, page: 0,
  }, 99), true);

  await assert.rejects(handleTelegramUpdate(message(407, "📰 Новини", 71), repo, telegram, undefined, undefined, navigation));

  assert.equal(navigation.screenIds.get(407), 99, "the existing ID survives a failed send");
  assert.deepEqual(telegram.deleted.map(({ messageId }) => messageId), [71], "only the recognized user button press was deleted");
});

test("About and unknown callbacks are handled in selected language", async () => {
  for (const [id, language, expected] of [
    [400, "ru", [/бесплатный информационный бот/, /Høringsportalen/, /Folketinget: мониторинг пока не активен/, /автоматические уведомления пока отключены/i, /Telegram ID/, /не является юридической консультацией/]],
    [401, "uk", [/безкоштовний інформаційний бот/, /Høringsportalen/, /Folketinget: моніторинг ще не активний/, /автоматичні сповіщення наразі вимкнені/i, /Telegram ID/, /не є юридичною консультацією/]],
  ] as const) {
    const repo = new MemoryRepository();
    const telegram = new MemoryTelegram();
    await handleTelegramUpdate(callback(id, `lang:${language}`), repo, telegram);
    await handleTelegramUpdate(callback(id, "menu:about"), repo, telegram, undefined, new MemorySourceStatus(new Date().toISOString()));
    const about = telegram.sent.at(-1)!.text;
    for (const phrase of expected) assert.match(about, phrase);
  assert.ok(Array.from(about).length <= 4096, "About screen fits Telegram message limit");
    assert.deepEqual(
      [repo.users.get(id)?.notify_direct, repo.users.get(id)?.notify_potential],
      [1, 0],
      "About does not change notification preferences",
    );
    const count = telegram.sent.length;
    await handleTelegramUpdate(callback(id, "future:unknown"), repo, telegram);
    assert.equal(telegram.sent.length, count);
  }
});

test("About shows unknown Høringsportalen status when source metadata fails", async () => {
  const repo = new MemoryRepository();
  const telegram = new MemoryTelegram();
  await handleTelegramUpdate(callback(402, "lang:ru"), repo, telegram);
  const brokenSourceStatus: SourceStatusRepository = {
    async lastSuccessfulHoeringsportalenRun() { throw new Error("database unavailable"); },
  };
  await handleTelegramUpdate(callback(402, "menu:about"), repo, telegram, undefined, brokenSourceStatus);
  const text = telegram.sent.at(-1)!.text;
  assert.match(text, /Høringsportalen: неизвестен/);
  assert.match(text, /Folketinget: мониторинг пока не активен/);
  assert.ok(Array.from(text).length <= 4096);
});

test("D1 repository uses the existing user fields and independent defaults", async () => {
  const db = new MemoryD1();
  const repo = new D1UserRepository(db as never);
  assert.equal(await repo.create(500, "uk"), true);
  assert.equal(await repo.create(500, "ru"), false);
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

test("authenticated webhook persists language and reply-screen state without removal messages", async () => {
  const db = new MemoryD1();
  const oldFetch = globalThis.fetch;
  const apiCalls: Array<{ url: string; payload: Record<string, unknown> }> = [];
  globalThis.fetch = async (input, init) => {
    apiCalls.push({
      url: String(input),
      payload: JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>,
    });
    if (String(input).endsWith("/sendMessage")) return Response.json({ ok: true, result: { message_id: apiCalls.length + 100 } });
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
      const screenCall = apiCalls.filter(({ url }) => url.endsWith("/sendMessage")).at(-1)!;
      assert.match(String(screenCall.payload.text), new RegExp(menuText));

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
        const startScreenCall = apiCalls.filter(({ url }) => url.endsWith("/sendMessage")).at(-1)!;
        assert.match(String(startScreenCall.payload.text), new RegExp(menuText));
      }
    }
    assert.equal(db.users.size, 2);
    assert.ok(apiCalls.length >= 8);
    const removalCalls = apiCalls.filter(({ payload }) => (payload.reply_markup as { remove_keyboard?: boolean } | undefined)?.remove_keyboard);
    assert.equal(removalCalls.length, 0, "ReplyKeyboardMarkup replaces an old keyboard directly");
    assert.ok(apiCalls.some(({ url }) => url.endsWith("/deleteMessage")), "tracked old screens are cleaned after sending a replacement");
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
  let screenFinishedBeforeAcknowledgement = false;
  telegram.answerCallback = async (id: string) => {
    await delay(90);
    telegram.answered.push(id);
    acknowledgementFinished = true;
  };
  telegram.sendScreenMessage = async (chatId, text, keyboard) => {
    await delay(15);
    telegram.sent.push({ chatId, text, keyboard });
    screenFinishedBeforeAcknowledgement = !acknowledgementFinished;
    return { message_id: 808 };
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
  screenFinishedBeforeAcknowledgement = false;
  telegram.answered.length = 0;
  telegram.edited.length = 0;
  const started = performance.now();
  try {
    await handleTelegramUpdate(callback(701, "nav:main"), slowRepository, telegram);
  } finally {
    console.info = originalInfo;
  }
  const totalMs = performance.now() - started;
  assert.equal(screenFinishedBeforeAcknowledgement, true);
  assert.equal(telegram.answered.length, 1);
  assert.equal(telegram.sent.length, 1);
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

test("rapid repeated navigation sends only one persisted screen and never deletes the winner", async () => {
  const repository = new MemoryRepository();
  repository.users.set(702, { telegram_id: 702, language: "ru", notify_direct: 1, notify_potential: 0 });
  const telegram = new MemoryTelegram();
  const navigation = new MemoryNavigation();
  await Promise.all([
    handleTelegramUpdate(callback(702, "nav:main"), repository, telegram, undefined, undefined, navigation),
    handleTelegramUpdate(callback(702, "nav:main"), repository, telegram, undefined, undefined, navigation),
  ]);
  assert.equal(telegram.answered.length, 2);
  assert.equal(telegram.sent.length, 2, "both sends may race before D1 compare-and-swap");
  assert.equal(telegram.deleted.length, 1, "the unclaimed competing screen is removed once");
  assert.notEqual(telegram.deleted[0].messageId, navigation.screenIds.get(702),
    "the currently persisted winning screen is never deleted");
  assert.ok(telegram.sent.every(({ text }) => text.includes("Выберите раздел")));
});
