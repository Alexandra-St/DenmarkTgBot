import type { Language, UserPreferences, UserRepository } from "../../database/users.ts";
import { t } from "../../localization/index.ts";
import type { TelegramCallbackQuery, TelegramMessage, TelegramUpdate } from "../../types/telegram.ts";
import type { TelegramClient } from "../telegram.ts";
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
): Promise<void> {
  const body = `${prefix ? `${prefix}\n\n` : ""}${t(language, "mainMenu")}`;
  if (messageId === undefined) await telegram.sendMessage(chatId, body, mainKeyboard(language));
  else await telegram.editMessage(chatId, messageId, body, mainKeyboard(language));
}

function notificationText(user: UserPreferences, language: Language): string {
  const direct = user.notify_direct ? t(language, "enabled") : t(language, "disabled");
  const potential = user.notify_potential ? t(language, "enabled") : t(language, "disabled");
  return `${t(language, "notificationsTitle")}\n\n${direct} ${t(language, "direct")}\n${potential} ${t(language, "potential")}`;
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

async function handleCallback(
  callback: TelegramCallbackQuery,
  repository: UserRepository,
  telegram: TelegramClient,
): Promise<void> {
  await telegram.answerCallback(callback.id);
  const message = callback.message;
  if (!message || !validTelegramId(callback.from.id) || !validTelegramId(message.chat?.id)) return;
  const data = callback.data ?? "";

  if (data === "lang:uk" || data === "lang:ru") {
    const language = data.slice(5) as Language;
    const existing = await repository.find(callback.from.id);
    if (existing) {
      await repository.updateLanguage(callback.from.id, language);
      await telegram.editMessage(
        message.chat.id,
        message.message_id,
        `${t(language, "languageSaved")}\n\n${t(language, "settingsTitle")}`,
        settingsKeyboard(language),
      );
    } else {
      await repository.create(callback.from.id, language);
      await showMain(telegram, message.chat.id, language, message.message_id, t(language, "languageSaved"));
    }
    return;
  }

  const user = await repository.find(callback.from.id);
  if (!user) {
    await telegram.editMessage(
      message.chat.id,
      message.message_id,
      languageScreen("uk"),
      languageKeyboard("uk"),
    );
    return;
  }
  const language = user.language;

  if (data === "nav:main") {
    await showMain(telegram, message.chat.id, language, message.message_id);
  } else if (data === "nav:settings" || data === "menu:settings") {
    await telegram.editMessage(
      message.chat.id,
      message.message_id,
      t(language, "settingsTitle"),
      settingsKeyboard(language),
    );
  } else if (data === "settings:language") {
    await telegram.editMessage(
      message.chat.id,
      message.message_id,
      t(language, "languageTitle"),
      settingsLanguageKeyboard(language),
    );
  } else if (data === "settings:notifications") {
    await telegram.editMessage(
      message.chat.id,
      message.message_id,
      notificationText(user, language),
      notificationKeyboard(language, Boolean(user.notify_direct), Boolean(user.notify_potential)),
    );
  } else if (data === "notify:direct" || data === "notify:potential") {
    const preference = data.slice("notify:".length) as "direct" | "potential";
    const current = preference === "direct" ? Boolean(user.notify_direct) : Boolean(user.notify_potential);
    await repository.updateNotification(callback.from.id, preference, !current);
    const updated = await repository.find(callback.from.id);
    if (!updated) throw new Error("User disappeared after preference update");
    await telegram.editMessage(
      message.chat.id,
      message.message_id,
      notificationText(updated, language),
      notificationKeyboard(language, Boolean(updated.notify_direct), Boolean(updated.notify_potential)),
    );
  } else if (data === "menu:news" || data === "menu:archive") {
    await telegram.editMessage(
      message.chat.id,
      message.message_id,
      t(language, "unavailable"),
      backToMainKeyboard(language),
    );
  } else if (data === "menu:about") {
    await telegram.editMessage(
      message.chat.id,
      message.message_id,
      t(language, "aboutText"),
      backToMainKeyboard(language),
    );
  }
}

export async function handleTelegramUpdate(
  update: TelegramUpdate,
  repository: UserRepository,
  telegram: TelegramClient,
): Promise<void> {
  if (update.message) {
    await handleMessage(update.message, repository, telegram);
    return;
  }
  if (update.callback_query) {
    await handleCallback(update.callback_query, repository, telegram);
  }
  // Unsupported Telegram update types are intentionally ignored.
}
