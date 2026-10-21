import type { Language, UserPreferences, UserRepository } from "../../database/users.ts";
import { t } from "../../localization/index.ts";
import type { InlineKeyboardMarkup, ReplyKeyboardMarkup, TelegramCallbackQuery, TelegramMessage, TelegramUpdate } from "../../types/telegram.ts";
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
  detailReplyKeyboard,
  languageKeyboard,
  mainKeyboard,
  notificationKeyboard,
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

async function readScreenMessageIds(repository: NavigationStateRepository | undefined, telegramId: number): Promise<{ keyboard: number | null; screen: number | null }> {
  try {
    const ids = await repository?.getScreenMessageIds(telegramId);
    if (!ids) return { keyboard: null, screen: null };
    const valid = (value: number | null) => typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : null;
    return { keyboard: valid(ids.keyboard), screen: valid(ids.screen) };
  } catch {
    return { keyboard: null, screen: null };
  }
}

async function safeDeleteMessage(telegram: TelegramClient, chatId: number, messageId: number | null | undefined): Promise<void> {
  if (messageId === undefined || messageId === null || !validTelegramId(messageId)) return;
  try { await telegram.deleteMessage(chatId, messageId); }
  catch { /* Deletion is best-effort and is never retried. */ }
}

/** Reply and inline keyboards belong on separate messages. Claim both IDs atomically before cleaning old screens. */
async function sendTrackedScreen(
  telegram: TelegramClient,
  navigation: NavigationStateRepository | undefined,
  telegramId: number,
  chatId: number,
  language: Language,
  state: UserNavigationState,
  text: string,
  markup: ReplyKeyboardMarkup,
  inlineMarkup?: InlineKeyboardMarkup,
): Promise<boolean> {
  const previous = await readScreenMessageIds(navigation, telegramId);
  let keyboard: { message_id: number } | undefined;
  let screen: { message_id: number } | undefined;
  try {
    keyboard = await telegram.sendScreenMessage(chatId, t(language, "navigationKeyboardActive"), markup);
    screen = await telegram.sendScreenMessage(chatId, text, inlineMarkup);
  } catch (error) {
    await safeDeleteMessage(telegram, chatId, screen?.message_id);
    await safeDeleteMessage(telegram, chatId, keyboard?.message_id);
    throw error;
  }
  if (!navigation) return true;

  let claimed = false;
  try {
    claimed = await navigation.replaceScreens(telegramId, previous, state, { keyboard: keyboard.message_id, screen: screen.message_id });
  } catch (error) {
    await safeDeleteMessage(telegram, chatId, screen.message_id);
    await safeDeleteMessage(telegram, chatId, keyboard.message_id);
    throw error;
  }
  if (!claimed) {
    // A faster request already claimed a newer pair. Remove only this unclaimed pair.
    await safeDeleteMessage(telegram, chatId, screen.message_id);
    await safeDeleteMessage(telegram, chatId, keyboard.message_id);
    return false;
  }
  if (previous.screen !== screen.message_id) await safeDeleteMessage(telegram, chatId, previous.screen);
  if (previous.keyboard !== keyboard.message_id) await safeDeleteMessage(telegram, chatId, previous.keyboard);
  return true;
}

async function replaceTrackedInlineScreen(
  telegram: TelegramClient,
  navigation: NavigationStateRepository | undefined,
  telegramId: number,
  chatId: number,
  messageId: number,
  state: UserNavigationState,
  text: string,
  inlineMarkup: InlineKeyboardMarkup,
): Promise<void> {
  const previous = await readScreenMessageIds(navigation, telegramId);
  if (navigation && previous.screen !== messageId) return;
  const sent = await telegram.sendScreenMessage(chatId, text, inlineMarkup);
  if (!navigation) return;
  let claimed = false;
  try {
    claimed = await navigation.replaceScreens(telegramId, previous, state, { keyboard: previous.keyboard, screen: sent.message_id });
  } catch (error) {
    await safeDeleteMessage(telegram, chatId, sent.message_id);
    throw error;
  }
  if (!claimed) {
    await safeDeleteMessage(telegram, chatId, sent.message_id);
    return;
  }
  await safeDeleteMessage(telegram, chatId, previous.screen);
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

async function showMain(
  telegram: TelegramClient,
  navigation: NavigationStateRepository | undefined,
  telegramId: number,
  chatId: number,
  language: Language,
  prefix?: string,
): Promise<void> {
  const body = `${prefix ? `${prefix}\n\n` : ""}${t(language, "mainMenu")}`;
  await sendTrackedScreen(telegram, navigation, telegramId, chatId, language, mainState(), body, mainKeyboard(language));
}

async function showPublicationList(
  telegram: TelegramClient,
  publications: PublicationRepository,
  navigation: NavigationStateRepository | undefined,
  telegramId: number,
  chatId: number,
  messageId: number | undefined,
  language: Language,
  context: { kind: "news"; page: number } | { kind: "archive"; filter: ArchiveFilter; page: number },
  metrics?: CallbackMetrics,
): Promise<void> {
  const screen = await timed(metrics, "publication_lookup_ms", () => publicationListScreen(publications, language, context));
  if (messageId === undefined) {
    await sendTrackedScreen(telegram, navigation, telegramId, chatId, language, listState(context), screen.text, sectionNavigationKeyboard(language), screen.keyboard);
  } else await timed(metrics, "telegram_edit_ms", () => replaceTrackedInlineScreen(telegram, navigation, telegramId, chatId, messageId, listState(context), screen.text, screen.keyboard));
}

async function showSettings(
  telegram: TelegramClient,
  navigation: NavigationStateRepository | undefined,
  telegramId: number,
  chatId: number,
  language: Language,
): Promise<void> {
  await sendTrackedScreen(telegram, navigation, telegramId, chatId, language, sectionState("settings"), t(language, "settingsTitle"), settingsKeyboard(language));
}

async function showLanguageSettings(telegram: TelegramClient, navigation: NavigationStateRepository | undefined, telegramId: number, chatId: number, language: Language): Promise<void> {
  const body = t(language, "languageTitle");
  const state: UserNavigationState = { ...sectionState("settings"), screen: "settings_language", parent_screen: "settings" };
  await sendTrackedScreen(telegram, navigation, telegramId, chatId, language, state, body, settingsLanguageKeyboard(language));
}

async function showNotificationSettings(
  telegram: TelegramClient,
  navigation: NavigationStateRepository | undefined,
  telegramId: number,
  chatId: number,
  user: UserPreferences,
): Promise<void> {
  const language = user.language;
  const body = notificationText(user, language);
  const keyboard = notificationKeyboard(language, Boolean(user.notify_direct), Boolean(user.notify_potential));
  const state: UserNavigationState = { ...sectionState("settings"), screen: "settings_notifications", parent_screen: "settings" };
  await sendTrackedScreen(telegram, navigation, telegramId, chatId, language, state, body, keyboard);
}

async function showAbout(
  telegram: TelegramClient,
  navigation: NavigationStateRepository | undefined,
  telegramId: number,
  chatId: number,
  language: Language,
  sourceStatus?: SourceStatusRepository,
): Promise<void> {
  let lastSuccessfulHoeringsportalenRun: string | null = null;
  try { lastSuccessfulHoeringsportalenRun = await sourceStatus?.lastSuccessfulHoeringsportalenRun() ?? null; }
  catch { /* Keep About available if metadata cannot be read. */ }
  const body = `${t(language, "aboutText")}\n\n${sourceStatusScreen(language, lastSuccessfulHoeringsportalenRun)}`;
  await sendTrackedScreen(telegram, navigation, telegramId, chatId, language, sectionState("about"), body, sectionNavigationKeyboard(language));
}

function publicationStateContext(state: UserNavigationState): { kind: "news"; page: number } | { kind: "archive"; filter: ArchiveFilter; page: number } {
  return savedListContext(state);
}

async function renderNavigationState(
  state: UserNavigationState,
  user: UserPreferences,
  navigation: NavigationStateRepository | undefined,
  chatId: number,
  telegramId: number,
  telegram: TelegramClient,
  publications?: PublicationRepository,
  sourceStatus?: SourceStatusRepository,
): Promise<void> {
  const language = user.language;
  if (state.screen === "main") await showMain(telegram, navigation, telegramId, chatId, language);
  else if (state.screen === "settings") await showSettings(telegram, navigation, telegramId, chatId, language);
  else if (state.screen === "settings_language") await showLanguageSettings(telegram, navigation, telegramId, chatId, language);
  else if (state.screen === "settings_notifications") await showNotificationSettings(telegram, navigation, telegramId, chatId, user);
  else if (state.screen === "about") await showAbout(telegram, navigation, telegramId, chatId, language, sourceStatus);
  else if (state.screen === "publication_detail") {
    const context = publicationStateContext(state);
    const body = publications ? (await publicationListScreen(publications, language, context)).text : t(language, "unavailable");
    await sendTrackedScreen(telegram, navigation, telegramId, chatId, language, state, body, detailReplyKeyboard(language));
  } else if (!publications) {
    const keyboard = sectionNavigationKeyboard(language);
    await sendTrackedScreen(telegram, navigation, telegramId, chatId, language, state, t(language, "unavailable"), keyboard);
  } else {
    const context = publicationStateContext(state);
    const screen = await publicationListScreen(publications, language, context);
    await sendTrackedScreen(telegram, navigation, telegramId, chatId, language, state, screen.text, sectionNavigationKeyboard(language), screen.keyboard);
  }
}

async function handleStart(
  message: TelegramMessage,
  repository: UserRepository,
  telegram: TelegramClient,
  navigation?: NavigationStateRepository,
): Promise<void> {
  if (message.chat.type !== "private" || !message.from || !validTelegramId(message.from.id) || !validTelegramId(message.chat?.id)) return;
  const user = await repository.find(message.from.id);
  if (user) {
    await showMain(telegram, navigation, user.telegram_id, message.chat.id, user.language);
  } else await telegram.sendMessage(message.chat.id, languageScreen("uk"), languageKeyboard("uk"));
}

async function handleHelp(message: TelegramMessage, repository: UserRepository, telegram: TelegramClient, navigation?: NavigationStateRepository): Promise<void> {
  if (message.chat.type !== "private" || !message.from || !validTelegramId(message.from.id) || !validTelegramId(message.chat?.id)) return;
  const user = await repository.find(message.from.id);
  if (user) {
    await sendTrackedScreen(
      telegram, navigation, user.telegram_id, message.chat.id, user.language, mainState(),
      `${t(user.language, "firstTimeWelcome")}\n\n${t(user.language, "mainMenu")}`,
      mainKeyboard(user.language),
    );
  } else await telegram.sendMessage(message.chat.id, languageScreen("uk"), languageKeyboard("uk"));
}

type ReplyAction =
  | "news" | "archive" | "settings" | "about" | "back" | "home"
  | "open:language" | "open:notifications"
  | "language:uk" | "language:ru"
  | "notify:direct:0" | "notify:direct:1" | "notify:potential:0" | "notify:potential:1";

function replyAction(text: string, state: UserNavigationState, user: UserPreferences): ReplyAction | null {
  const language = user.language;
  if (state.screen === "settings") {
    if (text === t(language, "language")) return "open:language";
    if (text === t(language, "notifications")) return "open:notifications";
    if (text === t(language, "back")) return "back";
    return null;
  }
  if (state.screen === "settings_language") {
    if (text === "Українська") return "language:uk";
    if (text === "Русский") return "language:ru";
    if (text === t(language, "back")) return "back";
    if (text === t(language, "main")) return "home";
    return null;
  }
  if (state.screen === "settings_notifications") {
    const buttons = notificationKeyboard(language, Boolean(user.notify_direct), Boolean(user.notify_potential)).keyboard;
    if (text === buttons[0]?.[0]) return user.notify_direct ? "notify:direct:0" : "notify:direct:1";
    if (text === buttons[1]?.[0]) return user.notify_potential ? "notify:potential:0" : "notify:potential:1";
    if (text === t(language, "back")) return "back";
    if (text === t(language, "main")) return "home";
    return null;
  }
  if (state.screen === "about") return text === t(language, "back") ? "back" : null;
  if (state.screen === "news" || state.screen === "archive") return text === t(language, "back") ? "back" : null;
  if (state.screen === "publication_detail") {
    if (text === t(language, "back")) return "back";
    if (text === t(language, "main")) return "home";
    return null;
  }

  const main = mainKeyboard(language).keyboard.flat();
  if (text === main[0]) return "news";
  if (text === main[1]) return "archive";
  if (text === main[2]) return "settings";
  if (text === main[3]) return "about";
  return null;
}

function sameNavigationState(left: UserNavigationState, right: UserNavigationState): boolean {
  return left.screen === right.screen && left.parent_screen === right.parent_screen &&
    left.publication_kind === right.publication_kind && left.archive_filter === right.archive_filter && left.page === right.page;
}

async function handleReplyAction(
  action: ReplyAction,
  current: UserNavigationState,
  user: UserPreferences,
  message: TelegramMessage,
  repository: UserRepository,
  telegram: TelegramClient,
  publications?: PublicationRepository,
  sourceStatus?: SourceStatusRepository,
  navigation?: NavigationStateRepository,
): Promise<void> {
  const id = user.telegram_id;
  const chatId = message.chat.id;
  if (action === "news" || action === "archive") {
    const context = action === "news" ? { kind: "news" as const, page: 0 } : { kind: "archive" as const, filter: "all" as const, page: 0 };
    const state = listState(context);
    if (sameNavigationState(current, state)) return;
    if (!publications) {
      await sendTrackedScreen(telegram, navigation, id, chatId, user.language, state, t(user.language, "unavailable"), sectionNavigationKeyboard(user.language));
    } else {
      const screen = await publicationListScreen(publications, user.language, context);
      await sendTrackedScreen(telegram, navigation, id, chatId, user.language, state, screen.text, sectionNavigationKeyboard(user.language), screen.keyboard);
    }
    return;
  }
  if (action === "settings") {
    const state = sectionState("settings");
    if (!sameNavigationState(current, state)) await showSettings(telegram, navigation, id, chatId, user.language);
    return;
  }
  if (action === "about") {
    const state = sectionState("about");
    if (!sameNavigationState(current, state)) await showAbout(telegram, navigation, id, chatId, user.language, sourceStatus);
    return;
  }
  if (action === "home") {
    if (!sameNavigationState(current, mainState())) await showMain(telegram, navigation, id, chatId, user.language);
    return;
  }
  if (action === "back") {
    const state = previousState(current);
    if (!sameNavigationState(current, state)) await renderNavigationState(state, user, navigation, chatId, id, telegram, publications, sourceStatus);
    return;
  }
  if (action === "language:uk" || action === "language:ru") {
    const language = action.slice("language:".length) as Language;
    if (user.language !== language) await repository.updateLanguage(id, language);
    const updated = await repository.find(id);
    if (!updated) return;
    await showSettings(telegram, navigation, id, chatId, updated.language);
    return;
  }
  if (action === "open:language") {
    await showLanguageSettings(telegram, navigation, id, chatId, user.language);
    return;
  }
  if (action === "open:notifications") {
    await showNotificationSettings(telegram, navigation, id, chatId, user);
    return;
  }
  const toggle = /^notify:(direct|potential):([01])$/u.exec(action);
  if (toggle) {
    const preference = toggle[1] as "direct" | "potential";
    const enabled = toggle[2] === "1";
    const currentValue = preference === "direct" ? Boolean(user.notify_direct) : Boolean(user.notify_potential);
    if (enabled === currentValue) return;
    await repository.updateNotification(id, preference, enabled);
    const updated = await repository.find(id);
    if (!updated) return;
    await showNotificationSettings(telegram, navigation, id, chatId, updated);
  }
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
  if (message.chat.type !== "private" || !message.from || !validTelegramId(message.from.id) || !validTelegramId(message.chat.id)) return;
  const user = await repository.find(message.from.id);
  if (!user) return;
  const state = await readNavigation(navigation, user.telegram_id);
  const action = replyAction(text ?? "", state, user);
  if (!action) return;
  // A reply-keyboard button is delivered as ordinary text; only current, recognized private-chat labels are deleted.
  await safeDeleteMessage(telegram, message.chat.id, message.message_id);
  await handleReplyAction(action, state, user, message, repository, telegram, publications, sourceStatus, navigation);
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
  if (!message || message.chat.type !== "private" || !validTelegramId(callback.from.id) || !validTelegramId(message.chat?.id)) return;
  const data = callback.data ?? "";

  if (data === "lang:uk" || data === "lang:ru") {
    const language = data.slice(5) as Language;
    const existing = await timed(metrics, "user_lookup_ms", () => repository.find(callback.from.id));
    if (existing) {
      await repository.updateLanguage(callback.from.id, language);
      const state = sectionState("settings");
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, t(language, "languageSaved"), clearInlineKeyboard()));
      await showSettings(telegram, navigation, callback.from.id, message.chat.id, language);
    } else {
      const created = await repository.create(callback.from.id, language);
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, t(language, "languageSaved"), clearInlineKeyboard()));
      await showMain(telegram, navigation, callback.from.id, message.chat.id, language, created ? t(language, "firstTimeWelcome") : undefined);
      if (created) await safeDeleteMessage(telegram, message.chat.id, message.message_id);
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
  const savedMessageIds = await readScreenMessageIds(navigation, callback.from.id);
  if (navigation && savedMessageIds.screen !== null && message.message_id !== savedMessageIds.screen && data !== "notification:main") return;

  if (data === "notification:main") {
    // Notification messages are immutable; this action posts a separate menu message.
    await showMain(telegram, navigation, callback.from.id, message.chat.id, language);
  } else if (data === "nav:main") {
    await showMain(telegram, navigation, callback.from.id, message.chat.id, language);
  } else if (data === "nav:back") {
    const state = previousState(current);
    await renderNavigationState(state, user, navigation, message.chat.id, callback.from.id, telegram, publications, sourceStatus);
  } else if (data === "nav:settings" || data === "menu:settings") {
    await showSettings(telegram, navigation, callback.from.id, message.chat.id, language);
  } else if (data === "settings:language") {
    await showLanguageSettings(telegram, navigation, callback.from.id, message.chat.id, language);
  } else if (data === "settings:language:uk" || data === "settings:language:ru") {
    const nextLanguage = data.endsWith(":uk") ? "uk" : "ru";
    await repository.updateLanguage(callback.from.id, nextLanguage);
    await showSettings(telegram, navigation, callback.from.id, message.chat.id, nextLanguage);
  } else if (data === "settings:notifications") {
    await showNotificationSettings(telegram, navigation, callback.from.id, message.chat.id, user);
  } else if (data.startsWith("notify:")) {
    const target = /^notify:(direct|potential):set:([01])$/u.exec(data);
    if (target) await repository.updateNotification(callback.from.id, target[1] as "direct" | "potential", target[2] === "1");
    const updated = await timed(metrics, "user_lookup_ms", () => repository.find(callback.from.id));
    if (!updated) throw new Error("User disappeared after preference update");
    await showNotificationSettings(telegram, navigation, callback.from.id, message.chat.id, updated);
  } else if (data === "menu:news" || data === "menu:archive") {
    const context = data === "menu:news" ? { kind: "news" as const, page: 0 } : { kind: "archive" as const, filter: "all" as const, page: 0 };
    if (!publications) {
      await sendTrackedScreen(telegram, navigation, callback.from.id, message.chat.id, language, listState(context), t(language, "unavailable"), sectionNavigationKeyboard(language));
    } else {
      await showPublicationList(telegram, publications, navigation, callback.from.id, message.chat.id, undefined, language, context, metrics);
    }
  } else if (/^(?:news:page:|archive:|pub:)/u.test(data)) {
    if (!publications || new TextEncoder().encode(data).length > 64) {
      const state = current.screen === "publication_detail" ? current : sectionState("news");
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, t(language, "publicationNotFound"), clearInlineKeyboard()));
      return;
    }
    const newsPage = /^news:page:(0|[1-9]\d{0,3})$/u.exec(data);
    const archiveFilter = /^archive:filter:(all|direct|potential):0$/u.exec(data);
    const archivePage = /^archive:page:(all|direct|potential):(0|[1-9]\d{0,3})$/u.exec(data);
    const detail = /^pub:d:([1-9]\d{0,15}):(news|archive):(all|direct|potential):(0|[1-9]\d{0,3})$/u.exec(data);
    if (newsPage && Number(newsPage[1]) <= MAX_PUBLICATION_PAGE) {
      const context = { kind: "news" as const, page: Number(newsPage[1]) };
      await showPublicationList(telegram, publications, navigation, callback.from.id, message.chat.id, message.message_id, language, context, metrics);
    } else if (archiveFilter) {
      const context = { kind: "archive" as const, filter: parseArchiveFilter(archiveFilter[1])!, page: 0 };
      await showPublicationList(telegram, publications, navigation, callback.from.id, message.chat.id, message.message_id, language, context, metrics);
    } else if (archivePage && Number(archivePage[2]) <= MAX_PUBLICATION_PAGE) {
      const context = { kind: "archive" as const, filter: parseArchiveFilter(archivePage[1])!, page: Number(archivePage[2]) };
      await showPublicationList(telegram, publications, navigation, callback.from.id, message.chat.id, message.message_id, language, context, metrics);
    } else if (detail && Number.isSafeInteger(Number(detail[1])) && Number(detail[4]) <= MAX_PUBLICATION_PAGE && (detail[2] !== "news" || detail[3] === "all")) {
      const context = detail[2] === "news"
        ? { kind: "news" as const, page: Number(detail[4]) }
        : { kind: "archive" as const, filter: parseArchiveFilter(detail[3])!, page: Number(detail[4]) };
      const screen = await timed(metrics, "publication_lookup_ms", () => publicationDetailScreen(publications, language, Number(detail[1]), context));
      await sendTrackedScreen(
        telegram, navigation, callback.from.id, message.chat.id, language, detailState(context), screen.text,
        detailReplyKeyboard(language),
        screen.keyboard.inline_keyboard.length ? screen.keyboard : undefined,
      );
    } else {
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, t(language, "publicationNotFound"), clearInlineKeyboard()));
    }
  } else if (data === "menu:about") {
    await showAbout(telegram, navigation, callback.from.id, message.chat.id, language, sourceStatus);
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
