import type { Language, UserPreferences, UserRepository } from "../../database/users.ts";
import { t } from "../../localization/index.ts";
import type { TelegramCallbackQuery, TelegramMessage, TelegramUpdate } from "../../types/telegram.ts";
import type { TelegramClient } from "../telegram.ts";
import type { PublicationRepository, ArchiveFilter } from "../../database/publications.ts";
import { MAX_PUBLICATION_PAGE } from "../../database/publications.ts";
import type { SourceStatusRepository } from "../../database/source_status.ts";
import { parseArchiveFilter, publicationDetailScreen, publicationListScreen } from "../publications.ts";
import { sourceStatusScreen } from "../source_status.ts";
import {
  backToMainKeyboard,
  languageKeyboard,
  mainKeyboard,
  notificationKeyboard,
  settingsLanguageKeyboard,
  settingsKeyboard,
} from "../keyboards/index.ts";

function validTelegramId(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function languageScreen(language: Language): string {
  return `${t(language, "welcome")}\n\n${t(language, "languagePrompt")}`;
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
  else await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(chatId, messageId, body, mainKeyboard(language)));
}

interface CallbackMetrics {
  callback_ack_ms: number;
  user_lookup_ms: number;
  publication_lookup_ms: number;
  telegram_edit_ms: number;
}

async function timed<T>(metrics: CallbackMetrics | undefined, field: keyof CallbackMetrics, operation: () => Promise<T>): Promise<T> {
  const started = performance.now();
  try {
    return await operation();
  } finally {
    if (metrics) metrics[field] += performance.now() - started;
  }
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

async function showNotificationSettings(
  telegram: TelegramClient,
  chatId: number,
  messageId: number,
  user: UserPreferences,
  metrics?: CallbackMetrics,
): Promise<void> {
  await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(
    chatId,
    messageId,
    notificationText(user, user.language),
    notificationKeyboard(user.language, Boolean(user.notify_direct), Boolean(user.notify_potential)),
  ));
}

async function showPublicationList(
  telegram: TelegramClient,
  publications: PublicationRepository,
  chatId: number,
  messageId: number,
  language: Language,
  context: { kind: "news"; page: number } | { kind: "archive"; filter: ArchiveFilter; page: number },
  metrics?: CallbackMetrics,
): Promise<void> {
  const screen = await timed(metrics, "publication_lookup_ms", () => publicationListScreen(publications, language, context));
  await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(chatId, messageId, screen.text, screen.keyboard));
}

function invalidPublicationNavigation(language: Language): { text: string; keyboard: ReturnType<typeof backToMainKeyboard> } {
  return { text: t(language, "publicationNotFound"), keyboard: backToMainKeyboard(language) };
}

async function handleStart(
  message: TelegramMessage,
  repository: UserRepository,
  telegram: TelegramClient,
): Promise<void> {
  if (!message.from || !validTelegramId(message.from.id) || !validTelegramId(message.chat?.id)) return;
  const user = await repository.find(message.from.id);
  if (user) await showMain(telegram, message.chat.id, user.language);
  else await telegram.sendMessage(message.chat.id, languageScreen("uk"), languageKeyboard("uk"));
}

async function handleMessage(
  message: TelegramMessage,
  repository: UserRepository,
  telegram: TelegramClient,
): Promise<void> {
  const command = message.text?.trim().split(/\s+/, 1)[0]?.split("@")[0].toLowerCase();
  if (command === "/start") await handleStart(message, repository, telegram);
}

async function processCallback(
  callback: TelegramCallbackQuery,
  repository: UserRepository,
  telegram: TelegramClient,
  publications?: PublicationRepository,
  sourceStatus?: SourceStatusRepository,
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
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(
        message.chat.id,
        message.message_id,
        `${t(language, "languageSaved")}\n\n${t(language, "settingsTitle")}`,
        settingsKeyboard(language),
      ));
    } else {
      const created = await repository.create(callback.from.id, language);
      if (created) await telegram.sendMessage(message.chat.id, t(language, "firstTimeWelcome"));
      else await repository.updateLanguage(callback.from.id, language);
      await showMain(telegram, message.chat.id, language, message.message_id, undefined, metrics);
    }
    return;
  }

  const user = await timed(metrics, "user_lookup_ms", () => repository.find(callback.from.id));
  if (!user) {
    await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(
      message.chat.id,
      message.message_id,
      languageScreen("uk"),
      languageKeyboard("uk"),
    ));
    return;
  }
  const language = user.language;

  if (data === "nav:main") {
    await showMain(telegram, message.chat.id, language, message.message_id, undefined, metrics);
  } else if (data === "nav:settings" || data === "menu:settings") {
    await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(
      message.chat.id,
      message.message_id,
      t(language, "settingsTitle"),
      settingsKeyboard(language),
    ));
  } else if (data === "settings:language") {
    await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(
      message.chat.id,
      message.message_id,
      t(language, "languageTitle"),
      settingsLanguageKeyboard(language),
    ));
  } else if (data === "settings:notifications") {
    await showNotificationSettings(telegram, message.chat.id, message.message_id, user, metrics);
  } else if (data.startsWith("notify:")) {
    const target = /^notify:(direct|potential):set:([01])$/.exec(data);
    if (target) {
      const preference = target[1] as "direct" | "potential";
      await repository.updateNotification(callback.from.id, preference, target[2] === "1");
    }
    // Legacy toggle payloads and malformed/stale notification callbacks only refresh the view.
    const updated = await timed(metrics, "user_lookup_ms", () => repository.find(callback.from.id));
    if (!updated) throw new Error("User disappeared after preference update");
    await showNotificationSettings(telegram, message.chat.id, message.message_id, updated, metrics);
  } else if (data === "menu:news" || data === "menu:archive") {
    if (!publications) {
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, t(language, "unavailable"), backToMainKeyboard(language)));
    } else if (data === "menu:news") {
      await showPublicationList(telegram, publications, message.chat.id, message.message_id, language, { kind: "news", page: 0 }, metrics);
    } else {
      await showPublicationList(telegram, publications, message.chat.id, message.message_id, language, { kind: "archive", filter: "all", page: 0 }, metrics);
    }
  } else if (/^(?:news:page:|archive:|pub:|menu:news|menu:archive)/u.test(data)) {
    if (!publications || new TextEncoder().encode(data).length > 64) {
      const invalid = invalidPublicationNavigation(language);
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, invalid.text, invalid.keyboard));
      return;
    }
    const newsPage = /^news:page:(0|[1-9]\d{0,3})$/u.exec(data);
    const archiveFilter = /^archive:filter:(all|direct|potential):0$/u.exec(data);
    const archivePage = /^archive:page:(all|direct|potential):(0|[1-9]\d{0,3})$/u.exec(data);
    const detail = /^pub:d:([1-9]\d{0,15}):(news|archive):(all|direct|potential):(0|[1-9]\d{0,3})$/u.exec(data);
    if (newsPage && Number(newsPage[1]) <= MAX_PUBLICATION_PAGE) {
      await showPublicationList(telegram, publications, message.chat.id, message.message_id, language, { kind: "news", page: Number(newsPage[1]) }, metrics);
    } else if (archiveFilter) {
      const filter = parseArchiveFilter(archiveFilter[1])!;
      await showPublicationList(telegram, publications, message.chat.id, message.message_id, language, { kind: "archive", filter, page: 0 }, metrics);
    } else if (archivePage && Number(archivePage[2]) <= MAX_PUBLICATION_PAGE) {
      const filter = parseArchiveFilter(archivePage[1])!;
      await showPublicationList(telegram, publications, message.chat.id, message.message_id, language, { kind: "archive", filter, page: Number(archivePage[2]) }, metrics);
    } else if (detail && Number.isSafeInteger(Number(detail[1])) && Number(detail[4]) <= MAX_PUBLICATION_PAGE && (detail[2] !== "news" || detail[3] === "all")) {
      const context = detail[2] === "news"
        ? { kind: "news" as const, page: Number(detail[4]) }
        : { kind: "archive" as const, filter: parseArchiveFilter(detail[3])!, page: Number(detail[4]) };
      const screen = await timed(metrics, "publication_lookup_ms", () => publicationDetailScreen(publications, language, Number(detail[1]), context));
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, screen.text, screen.keyboard));
    } else {
      const invalid = invalidPublicationNavigation(language);
      await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(message.chat.id, message.message_id, invalid.text, invalid.keyboard));
    }
  } else if (data === "menu:about") {
    let lastSuccessfulHoeringsportalenRun: string | null = null;
    try {
      lastSuccessfulHoeringsportalenRun = await sourceStatus?.lastSuccessfulHoeringsportalenRun() ?? null;
    } catch {
      // About must stay available even if source metadata cannot be read.
    }
    await timed(metrics, "telegram_edit_ms", () => telegram.editMessage(
      message.chat.id,
      message.message_id,
      `${t(language, "aboutText")}\n\n${sourceStatusScreen(language, lastSuccessfulHoeringsportalenRun)}`,
      backToMainKeyboard(language),
    ));
  }
}

async function handleCallback(
  callback: TelegramCallbackQuery,
  repository: UserRepository,
  telegram: TelegramClient,
  publications?: PublicationRepository,
  sourceStatus?: SourceStatusRepository,
): Promise<void> {
  const started = performance.now();
  const metrics: CallbackMetrics = { callback_ack_ms: 0, user_lookup_ms: 0, publication_lookup_ms: 0, telegram_edit_ms: 0 };
  // Start acknowledgement before any D1 work, but let the UI path proceed concurrently.
  // Convert rejection to a safe category immediately so it cannot become unhandled.
  const acknowledgement = timed(metrics, "callback_ack_ms", () => telegram.answerCallback(callback.id))
    .then(() => "ok" as const, () => "telegram_api_error" as const);
  let processingError: unknown;
  try {
    await processCallback(callback, repository, telegram, publications, sourceStatus, metrics);
  } catch (error) {
    processingError = error;
  }
  const acknowledgementResult = await acknowledgement;
  const success = processingError ? "failure" : "success";
  console.info(JSON.stringify({
    event: "telegram_callback_timing",
    route: routeCategory(callback.data ?? ""),
    callback_ack_ms: Math.round(metrics.callback_ack_ms),
    user_lookup_ms: Math.round(metrics.user_lookup_ms),
    publication_lookup_ms: Math.round(metrics.publication_lookup_ms),
    telegram_edit_ms: Math.round(metrics.telegram_edit_ms),
    total_ms: Math.round(performance.now() - started),
    success,
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
): Promise<void> {
  if (update.message) {
    await handleMessage(update.message, repository, telegram);
    return;
  }
  if (update.callback_query) {
    await handleCallback(update.callback_query, repository, telegram, publications, sourceStatus);
  }
  // Unsupported Telegram update types are intentionally ignored.
}
