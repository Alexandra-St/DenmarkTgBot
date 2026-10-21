import type { Language, UserPreferences, UserRepository } from "../../database/users.ts";
import { t } from "../../localization/index.ts";
import type { TelegramCallbackQuery, TelegramMessage, TelegramUpdate } from "../../types/telegram.ts";
import type { TelegramClient } from "../telegram.ts";
import type { PublicationRepository, ArchiveFilter } from "../../database/publications.ts";
import { MAX_PUBLICATION_PAGE } from "../../database/publications.ts";
import type { SourceStatusRepository } from "../../database/source_status.ts";
import {
  MAIN_NAVIGATION_STATE,
  isUserNavigationState,
  type NavigationParent,
  type NavigationStateRepository,
  type UserNavigationState,
} from "../../database/navigation_state.ts";
import { parseArchiveFilter, publicationDetailScreen, publicationListScreen } from "../publications.ts";
import { sourceStatusScreen } from "../source_status.ts";
import {
  detailNavigationKeyboard,
  languageKeyboard,
  mainKeyboard,
  notificationKeyboard,
  notificationToggleText,
  persistentReplyKeyboard,
  sectionNavigationKeyboard,
  settingsKeyboard,
  settingsLanguageKeyboard,
} from "../keyboards/index.ts";

function validTelegramId(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function languageScreen(language: Language): string {
  return `${t(language, "welcome")}\n\n${t(language, "languagePrompt")}`;
}

interface CallbackMetrics {
  callback_ack_ms: number;
  user_lookup_ms: number;
  publication_lookup_ms: number;
  telegram_edit_ms: number;
}

async function timed<T>(metrics: CallbackMetrics | undefined, field: keyof CallbackMetrics, operation: () => Promise<T>): Promise<T> {
  const started = performance.now();
  try { return await operation(); }
  finally { if (metrics) metrics[field] += performance.now() - started; }
}

function clearInlineKeyboard() { return { inline_keyboard: [] }; }

async function readNavigation(repository: NavigationStateRepository | undefined, telegramId: number): Promise<UserNavigationState> {
  try {
    const value = await repository?.get(telegramId);
    return value && isUserNavigationState(value) ? value : MAIN_NAVIGATION_STATE;
  } catch {
    return MAIN_NAVIGATION_STATE;
  }
}

async function writeNavigation(repository: NavigationStateRepository | undefined, telegramId: number, state: UserNavigationState): Promise<void> {
  if (!repository) return;
  await repository.set(telegramId, state);
}

function mainState(): UserNavigationState { return { ...MAIN_NAVIGATION_STATE }; }

function sectionState(screen: "news" | "archive" | "settings" | "about"): UserNavigationState {
  return {
    screen,
    parent_screen: "main",
    publication_kind: screen === "news" || screen === "archive" ? screen : null,
    archive_filter: screen === "archive" ? "all" : null,
    page: 0,
  };
}

function listState(context: { kind: "news"; page: number } | { kind: "archive"; filter: ArchiveFilter; page: number }): UserNavigationState {
  return {
    screen: context.kind,
    parent_screen: "main",
    publication_kind: context.kind,
    archive_filter: context.kind === "archive" ? context.filter : null,
    page: context.page,
  };
}

function detailState(context: { kind: "news"; page: number } | { kind: "archive"; filter: ArchiveFilter; page: number }): UserNavigationState {
  return {
    screen: "publication_detail",
    parent_screen: context.kind,
    publication_kind: context.kind,
    archive_filter: context.kind === "archive" ? context.filter : null,
    page: context.page,
  };
}

function savedListContext(state: UserNavigationState): { kind: "news"; page: number } | { kind: "archive"; filter: ArchiveFilter; page: number } {
  if (state.publication_kind === "archive") return {
    kind: "archive",
    filter: state.archive_filter ?? "all",
    page: state.page,
  };
  return { kind: "news", page: state.page };
}

function previousState(state: UserNavigationState): UserNavigationState {
  if (state.screen === "settings_language" || state.screen === "settings_notifications") return sectionState("settings");
  if (state.screen === "publication_detail" && (state.parent_screen === "news" || state.parent_screen === "archive")) {
    return listState(savedListContext(state));
  }
  return mainState();
}

function routeCategory(data: string): string {
  if (data.startsWith("lang:")) return "language";
  if (data.startsWith("news:") || data.startsWith("archive:") || data.startsWith("pub:")) return "publications";
  if (data.startsWith("notify:") || data.startsWith("settings:")) return "settings";
  if (data.startsWith("nav:") || data.startsWith("menu:")) return "menu";
  return "other";
}

function notificationText(user: UserPreferences, language: Language): string {
  const direct = user.notify_direct ? t(language, "enabled") : t(language, "disabled");
  const potential = user.notify_potential ? t(language, "enabled") : t(language, "disabled");
  return `${t(language, "notificationsTitle")}\n\n${direct} ${t(language, "direct")}\n${potential} ${t(language, "potential")}`;
}

async function setKeyboard(
  telegram: TelegramClient,
  chatId: number,
  language: Language,
  keyboard: ReturnType<typeof mainKeyboard>,
  text = t(language, "replyKeyboardReady"),
): Promise<void> {
  await telegram.sendMessage(chatId, text, keyboard);
}

async function showMain(
  telegram: TelegramClient,
  chatId: number,
  language: Language,
  messageId?: number,
  prefix?: string,
  metrics?: CallbackMetrics,
): Promise<void> {
  const body = `${prefix ? `${prefix}\n\n` : ""}${t(language, "mainMenu")}`;
  if (messageId === undefined) await telegram.sendMessage(chatId, body, mainKeyboard(language));
  else {
    await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(chatId, messageId, body, clearInlineKeyboard()));
    await setKeyboard(telegram, chatId, language, mainKeyboard(language));
  }
}

async function showPublicationList(
  telegram: TelegramClient,
  publications: PublicationRepository,
  chatId: number,
  messageId: number | undefined,
  language: Language,
  context: { kind: "news"; page: number } | { kind: "archive"; filter: ArchiveFilter; page: number },
  metrics?: CallbackMetrics,
): Promise<void> {
  const screen = await timed(metrics, "publication_lookup_ms", () => publicationListScreen(publications, language, context));
  if (messageId === undefined) {
    await telegram.sendMessage(chatId, screen.text, screen.keyboard);
    await setKeyboard(telegram, chatId, language, sectionNavigationKeyboard(language));
  } else await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(chatId, messageId, screen.text, screen.keyboard));
}

async function showSettings(
  telegram: TelegramClient,
  chatId: number,
  language: Language,
  messageId?: number,
  metrics?: CallbackMetrics,
): Promise<void> {
  if (messageId === undefined) await telegram.sendMessage(chatId, t(language, "settingsTitle"), settingsKeyboard(language));
  else {
    await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(chatId, messageId, t(language, "settingsTitle"), clearInlineKeyboard()));
    await setKeyboard(telegram, chatId, language, settingsKeyboard(language));
  }
}

async function showLanguageSettings(telegram: TelegramClient, chatId: number, language: Language, messageId?: number, metrics?: CallbackMetrics): Promise<void> {
  const body = t(language, "languageTitle");
  if (messageId === undefined) await telegram.sendMessage(chatId, body, settingsLanguageKeyboard(language));
  else {
    await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(chatId, messageId, body, clearInlineKeyboard()));
    await setKeyboard(telegram, chatId, language, settingsLanguageKeyboard(language));
  }
}

async function showNotificationSettings(
  telegram: TelegramClient,
  chatId: number,
  user: UserPreferences,
  messageId?: number,
  metrics?: CallbackMetrics,
): Promise<void> {
  const language = user.language;
  const body = notificationText(user, language);
  const keyboard = notificationKeyboard(language, Boolean(user.notify_direct), Boolean(user.notify_potential));
  if (messageId === undefined) await telegram.sendMessage(chatId, body, keyboard);
  else {
    await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(chatId, messageId, body, clearInlineKeyboard()));
    await setKeyboard(telegram, chatId, language, keyboard);
  }
}

async function showAbout(
  telegram: TelegramClient,
  chatId: number,
  language: Language,
  sourceStatus?: SourceStatusRepository,
  messageId?: number,
  metrics?: CallbackMetrics,
): Promise<void> {
  let lastSuccessfulHoeringsportalenRun: string | null = null;
  try { lastSuccessfulHoeringsportalenRun = await sourceStatus?.lastSuccessfulHoeringsportalenRun() ?? null; }
  catch { /* Keep About available if metadata cannot be read. */ }
  const body = `${t(language, "aboutText")}\n\n${sourceStatusScreen(language, lastSuccessfulHoeringsportalenRun)}`;
  if (messageId === undefined) await telegram.sendMessage(chatId, body, sectionNavigationKeyboard(language));
  else {
    await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(chatId, messageId, body, clearInlineKeyboard()));
    await setKeyboard(telegram, chatId, language, sectionNavigationKeyboard(language));
  }
}

function publicationStateContext(state: UserNavigationState): { kind: "news"; page: number } | { kind: "archive"; filter: ArchiveFilter; page: number } {
  return savedListContext(state);
}

function keyForText(text: string): string | null {
  const keys = ["news", "archive", "settings", "about", "back", "main", "language", "notifications"] as const;
  for (const key of keys) if (text === t("uk", key) || text === t("ru", key)) return key;
  if (text === "Українська") return "lang_uk";
  if (text === "Русский") return "lang_ru";
  for (const language of ["uk", "ru"] as const) {
    for (const preference of ["direct", "potential"] as const) {
      for (const enabled of [false, true]) {
        if (text === notificationToggleText(language, preference, enabled)) return `toggle_${preference}`;
      }
    }
  }
  return null;
}

async function renderNavigationState(
  state: UserNavigationState,
  user: UserPreferences,
  chatId: number,
  telegram: TelegramClient,
  publications?: PublicationRepository,
  sourceStatus?: SourceStatusRepository,
): Promise<void> {
  const language = user.language;
  if (state.screen === "main") await showMain(telegram, chatId, language);
  else if (state.screen === "settings") await showSettings(telegram, chatId, language);
  else if (state.screen === "settings_language") await showLanguageSettings(telegram, chatId, language);
  else if (state.screen === "settings_notifications") await showNotificationSettings(telegram, chatId, user);
  else if (state.screen === "about") await showAbout(telegram, chatId, language, sourceStatus);
  else if (state.screen === "publication_detail") {
    const context = publicationStateContext(state);
    if (!publications) {
      await telegram.sendMessage(chatId, t(language, "unavailable"), detailNavigationKeyboard(language));
    } else {
      const screen = await publicationListScreen(publications, language, context);
      await telegram.sendMessage(chatId, screen.text, screen.keyboard);
      await setKeyboard(telegram, chatId, language, detailNavigationKeyboard(language));
    }
  } else if (!publications) {
    await telegram.sendMessage(chatId, t(language, "unavailable"), sectionNavigationKeyboard(language));
  } else await showPublicationList(telegram, publications, chatId, undefined, language, publicationStateContext(state));
}

async function handleStart(
  message: TelegramMessage,
  repository: UserRepository,
  telegram: TelegramClient,
  navigation?: NavigationStateRepository,
): Promise<void> {
  if (!message.from || !validTelegramId(message.from.id) || !validTelegramId(message.chat?.id)) return;
  const user = await repository.find(message.from.id);
  if (user) {
    await writeNavigation(navigation, user.telegram_id, mainState());
    await showMain(telegram, message.chat.id, user.language);
  } else await telegram.sendMessage(message.chat.id, languageScreen("uk"), languageKeyboard("uk"));
}

async function handleHelp(message: TelegramMessage, repository: UserRepository, telegram: TelegramClient, navigation?: NavigationStateRepository): Promise<void> {
  if (!message.from || !validTelegramId(message.from.id) || !validTelegramId(message.chat?.id)) return;
  const user = await repository.find(message.from.id);
  if (user) {
    await writeNavigation(navigation, user.telegram_id, mainState());
    await telegram.sendMessage(message.chat.id, t(user.language, "firstTimeWelcome"), mainKeyboard(user.language));
  } else await telegram.sendMessage(message.chat.id, languageScreen("uk"), languageKeyboard("uk"));
}

async function handleMessage(
  message: TelegramMessage,
  repository: UserRepository,
  telegram: TelegramClient,
  publications?: PublicationRepository,
  sourceStatus?: SourceStatusRepository,
  navigation?: NavigationStateRepository,
): Promise<void> {
  const text = message.text?.trim();
  const command = text?.split(/\s+/, 1)[0]?.split("@")[0].toLowerCase();
  if (command === "/start") return handleStart(message, repository, telegram, navigation);
  if (command === "/help") return handleHelp(message, repository, telegram, navigation);
  if (!text || !message.from || !validTelegramId(message.from.id) || !validTelegramId(message.chat?.id)) return;

  const user = await repository.find(message.from.id);
  if (!user) {
    if (keyForText(text)) await telegram.sendMessage(message.chat.id, languageScreen("uk"), languageKeyboard("uk"));
    return;
  }
  const key = keyForText(text);
  if (!key) return;
  let state = await readNavigation(navigation, user.telegram_id);

  if (key === "main") state = mainState();
  else if (key === "back") state = previousState(state);
  else if (key === "news" || key === "archive" || key === "settings" || key === "about") state = sectionState(key);
  else if (state.screen === "settings" && key === "language") state = { ...sectionState("settings"), screen: "settings_language", parent_screen: "settings" };
  else if (state.screen === "settings" && key === "notifications") state = { ...sectionState("settings"), screen: "settings_notifications", parent_screen: "settings" };
  else if (state.screen === "settings_language" && (key === "lang_uk" || key === "lang_ru")) {
    const language = key === "lang_uk" ? "uk" : "ru";
    await repository.updateLanguage(user.telegram_id, language);
    state = sectionState("settings");
    await writeNavigation(navigation, user.telegram_id, state);
    await showSettings(telegram, message.chat.id, language);
    return;
  } else if (state.screen === "settings_notifications" && (key === "toggle_direct" || key === "toggle_potential")) {
    const preference = key === "toggle_direct" ? "direct" : "potential";
    await repository.updateNotification(user.telegram_id, preference, !Boolean(user[preference === "direct" ? "notify_direct" : "notify_potential"]));
    const updated = await repository.find(user.telegram_id);
    if (!updated) return;
    state = { ...state, screen: "settings_notifications", parent_screen: "settings" };
    await writeNavigation(navigation, user.telegram_id, state);
    await showNotificationSettings(telegram, message.chat.id, updated);
    return;
  } else {
    // Missing, stale, or irrelevant navigation input safely returns to the root.
    state = mainState();
  }

  await writeNavigation(navigation, user.telegram_id, state);
  await renderNavigationState(state, user, message.chat.id, telegram, publications, sourceStatus);
}

async function processCallback(
  callback: TelegramCallbackQuery,
  repository: UserRepository,
  telegram: TelegramClient,
  publications?: PublicationRepository,
  sourceStatus?: SourceStatusRepository,
  navigation?: NavigationStateRepository,
  metrics?: CallbackMetrics,
): Promise<void> {
  const message = callback.message;
  if (!message || !validTelegramId(callback.from.id) || !validTelegramId(message.chat?.id)) return;
  const data = callback.data ?? "";

  if (data === "lang:uk" || data === "lang:ru") {
    const language = data.slice(5) as Language;
    const existing = await timed(metrics, "user_lookup_ms", () => repository.find(callback.from.id));
    if (existing) {
      await repository.updateLanguage(callback.from.id, language);
      const state = sectionState("settings");
      await writeNavigation(navigation, callback.from.id, state);
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, t(language, "languageSaved"), clearInlineKeyboard()));
      await showSettings(telegram, message.chat.id, language);
    } else {
      const created = await repository.create(callback.from.id, language);
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, t(language, "languageSaved"), clearInlineKeyboard()));
      await writeNavigation(navigation, callback.from.id, mainState());
      if (created) await telegram.sendMessage(message.chat.id, t(language, "firstTimeWelcome"), mainKeyboard(language));
      await showMain(telegram, message.chat.id, language, undefined, undefined, metrics);
    }
    return;
  }

  const user = await timed(metrics, "user_lookup_ms", () => repository.find(callback.from.id));
  if (!user) {
    await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, languageScreen("uk"), languageKeyboard("uk")));
    return;
  }
  const language = user.language;
  const current = await readNavigation(navigation, callback.from.id);
  const save = async (state: UserNavigationState) => writeNavigation(navigation, callback.from.id, state);

  if (data === "nav:main") {
    await save(mainState());
    await showMain(telegram, message.chat.id, language, message.message_id, undefined, metrics);
  } else if (data === "nav:settings" || data === "menu:settings") {
    await save(sectionState("settings"));
    await showSettings(telegram, message.chat.id, language, message.message_id, metrics);
  } else if (data === "settings:language") {
    const state: UserNavigationState = { ...sectionState("settings"), screen: "settings_language", parent_screen: "settings" };
    await save(state);
    await showLanguageSettings(telegram, message.chat.id, language, message.message_id, metrics);
  } else if (data === "settings:notifications") {
    const state: UserNavigationState = { ...sectionState("settings"), screen: "settings_notifications", parent_screen: "settings" };
    await save(state);
    await showNotificationSettings(telegram, message.chat.id, user, message.message_id, metrics);
  } else if (data.startsWith("notify:")) {
    const target = /^notify:(direct|potential):set:([01])$/u.exec(data);
    if (target) await repository.updateNotification(callback.from.id, target[1] as "direct" | "potential", target[2] === "1");
    const updated = await timed(metrics, "user_lookup_ms", () => repository.find(callback.from.id));
    if (!updated) throw new Error("User disappeared after preference update");
    const state: UserNavigationState = { ...sectionState("settings"), screen: "settings_notifications", parent_screen: "settings" };
    await save(state);
    await showNotificationSettings(telegram, message.chat.id, updated, message.message_id, metrics);
  } else if (data === "menu:news" || data === "menu:archive") {
    const context = data === "menu:news" ? { kind: "news" as const, page: 0 } : { kind: "archive" as const, filter: "all" as const, page: 0 };
    await save(listState(context));
    if (!publications) {
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, t(language, "unavailable"), clearInlineKeyboard()));
      await setKeyboard(telegram, message.chat.id, language, sectionNavigationKeyboard(language));
    } else {
      await showPublicationList(telegram, publications, message.chat.id, message.message_id, language, context, metrics);
      await setKeyboard(telegram, message.chat.id, language, sectionNavigationKeyboard(language));
    }
  } else if (/^(?:news:page:|archive:|pub:)/u.test(data)) {
    if (!publications || new TextEncoder().encode(data).length > 64) {
      const state = current.screen === "publication_detail" ? current : sectionState("news");
      await save(state);
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, t(language, "publicationNotFound"), clearInlineKeyboard()));
      await setKeyboard(telegram, message.chat.id, language, state.screen === "publication_detail" ? detailNavigationKeyboard(language) : sectionNavigationKeyboard(language));
      return;
    }
    const newsPage = /^news:page:(0|[1-9]\d{0,3})$/u.exec(data);
    const archiveFilter = /^archive:filter:(all|direct|potential):0$/u.exec(data);
    const archivePage = /^archive:page:(all|direct|potential):(0|[1-9]\d{0,3})$/u.exec(data);
    const detail = /^pub:d:([1-9]\d{0,15}):(news|archive):(all|direct|potential):(0|[1-9]\d{0,3})$/u.exec(data);
    if (newsPage && Number(newsPage[1]) <= MAX_PUBLICATION_PAGE) {
      const context = { kind: "news" as const, page: Number(newsPage[1]) };
      await save(listState(context));
      await showPublicationList(telegram, publications, message.chat.id, message.message_id, language, context, metrics);
    } else if (archiveFilter) {
      const context = { kind: "archive" as const, filter: parseArchiveFilter(archiveFilter[1])!, page: 0 };
      await save(listState(context));
      await showPublicationList(telegram, publications, message.chat.id, message.message_id, language, context, metrics);
    } else if (archivePage && Number(archivePage[2]) <= MAX_PUBLICATION_PAGE) {
      const context = { kind: "archive" as const, filter: parseArchiveFilter(archivePage[1])!, page: Number(archivePage[2]) };
      await save(listState(context));
      await showPublicationList(telegram, publications, message.chat.id, message.message_id, language, context, metrics);
    } else if (detail && Number.isSafeInteger(Number(detail[1])) && Number(detail[4]) <= MAX_PUBLICATION_PAGE && (detail[2] !== "news" || detail[3] === "all")) {
      const context = detail[2] === "news"
        ? { kind: "news" as const, page: Number(detail[4]) }
        : { kind: "archive" as const, filter: parseArchiveFilter(detail[3])!, page: Number(detail[4]) };
      await save(detailState(context));
      const screen = await timed(metrics, "publication_lookup_ms", () => publicationDetailScreen(publications, language, Number(detail[1]), context));
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, screen.text, screen.keyboard));
      await setKeyboard(telegram, message.chat.id, language, detailNavigationKeyboard(language));
    } else {
      await save(current.screen === "publication_detail" ? current : sectionState("news"));
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, t(language, "publicationNotFound"), clearInlineKeyboard()));
    }
  } else if (data === "menu:about") {
    await save(sectionState("about"));
    await showAbout(telegram, message.chat.id, language, sourceStatus, message.message_id, metrics);
  }
}

async function handleCallback(
  callback: TelegramCallbackQuery,
  repository: UserRepository,
  telegram: TelegramClient,
  publications?: PublicationRepository,
  sourceStatus?: SourceStatusRepository,
  navigation?: NavigationStateRepository,
): Promise<void> {
  const started = performance.now();
  const metrics: CallbackMetrics = { callback_ack_ms: 0, user_lookup_ms: 0, publication_lookup_ms: 0, telegram_edit_ms: 0 };
  const acknowledgement = timed(metrics, "callback_ack_ms", () => telegram.answerCallback(callback.id))
    .then(() => "ok" as const, () => "telegram_api_error" as const);
  let processingError: unknown;
  try { await processCallback(callback, repository, telegram, publications, sourceStatus, navigation, metrics); }
  catch (error) { processingError = error; }
  const acknowledgementResult = await acknowledgement;
  console.info(JSON.stringify({
    event: "telegram_callback_timing",
    route: routeCategory(callback.data ?? ""),
    callback_ack_ms: Math.round(metrics.callback_ack_ms),
    user_lookup_ms: Math.round(metrics.user_lookup_ms),
    publication_lookup_ms: Math.round(metrics.publication_lookup_ms),
    telegram_edit_ms: Math.round(metrics.telegram_edit_ms),
    total_ms: Math.round(performance.now() - started),
    success: processingError ? "failure" : "success",
    ...(acknowledgementResult === "ok" ? {} : { acknowledgement: acknowledgementResult }),
  }));
  if (processingError) throw processingError;
}

export async function handleTelegramUpdate(
  update: TelegramUpdate,
  repository: UserRepository,
  telegram: TelegramClient,
  publications?: PublicationRepository,
  sourceStatus?: SourceStatusRepository,
  navigation?: NavigationStateRepository,
): Promise<void> {
  if (update.message) {
    await handleMessage(update.message, repository, telegram, publications, sourceStatus, navigation);
    return;
  }
  if (update.callback_query) await handleCallback(update.callback_query, repository, telegram, publications, sourceStatus, navigation);
}
