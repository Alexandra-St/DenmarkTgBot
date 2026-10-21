import type { Language } from "../../database/users.ts";
import { t } from "../../localization/index.ts";
import type { InlineKeyboardMarkup, ReplyKeyboardMarkup } from "../../types/telegram.ts";
import type { ArchiveFilter, PublicationRecord } from "../../database/publications.ts";

export type PublicationListContext =
  | { kind: "news"; page: number }
  | { kind: "archive"; filter: ArchiveFilter; page: number };

const keyboard = (...rows: InlineKeyboardMarkup["inline_keyboard"]): InlineKeyboardMarkup => ({
  inline_keyboard: rows,
});

export function languageKeyboard(_language: Language): InlineKeyboardMarkup {
  return keyboard(
    [{ text: "Українська", callback_data: "lang:uk" }],
    [{ text: "Русский", callback_data: "lang:ru" }],
  );
}

export function settingsLanguageKeyboard(language: Language): ReplyKeyboardMarkup {
  return replyKeyboard([
    [{ text: "Українська" }, { text: "Русский" }],
    [{ text: t(language, "back") }],
    [{ text: t(language, "main") }],
  ]);
}

export function mainKeyboard(language: Language): ReplyKeyboardMarkup {
  return persistentReplyKeyboard(language);
}

export function persistentReplyKeyboard(language: Language): ReplyKeyboardMarkup {
  return replyKeyboard([
    [{ text: t(language, "news") }, { text: t(language, "archive") }],
    [{ text: t(language, "settings") }, { text: t(language, "about") }],
  ]);
}

function replyKeyboard(rows: ReplyKeyboardMarkup["keyboard"]): ReplyKeyboardMarkup {
  return { keyboard: rows, is_persistent: true, resize_keyboard: true };
}

export function sectionNavigationKeyboard(language: Language): ReplyKeyboardMarkup {
  return replyKeyboard([[{ text: t(language, "back") }]]);
}

export function detailNavigationKeyboard(language: Language): ReplyKeyboardMarkup {
  return replyKeyboard([[{ text: t(language, "back") }, { text: t(language, "main") }]]);
}

export function settingsKeyboard(language: Language): ReplyKeyboardMarkup {
  return replyKeyboard([
    [{ text: t(language, "language") }, { text: t(language, "notifications") }],
    [{ text: t(language, "back") }],
  ]);
}

export function notificationToggleText(language: Language, preference: "direct" | "potential", enabled: boolean): string {
  const label = preference === "direct" ? t(language, "directShort") : t(language, "potentialShort");
  return `${enabled ? "✅" : "⬜"} ${label} — ${t(language, enabled ? "enabledState" : "disabledState")}`;
}

export function notificationKeyboard(
  language: Language,
  direct: boolean,
  potential: boolean,
): ReplyKeyboardMarkup {
  return replyKeyboard([
    [{ text: notificationToggleText(language, "direct", direct) }],
    [{ text: notificationToggleText(language, "potential", potential) }],
    [{ text: t(language, "back") }, { text: t(language, "main") }],
  ]);
}

function clipped(value: string, maxCodePoints: number): string {
  const chars = Array.from(value);
  return chars.length > maxCodePoints ? `${chars.slice(0, maxCodePoints - 1).join("")}…` : value;
}

function detailCallback(id: number, context: PublicationListContext): string {
  return context.kind === "news"
    ? `pub:d:${id}:news:all:${context.page}`
    : `pub:d:${id}:archive:${context.filter}:${context.page}`;
}

export function publicationListKeyboard(
  language: Language,
  items: PublicationRecord[],
  hasNext: boolean,
  context: PublicationListContext,
): InlineKeyboardMarkup {
  const rows: InlineKeyboardMarkup["inline_keyboard"] = items.map((item) => [{
    text: `${item.relevance === "DIRECT" ? "🟢" : "🟡"} ${clipped(item.original_title.replace(/\s+/gu, " ").trim(), 48)}`,
    callback_data: detailCallback(item.id, context),
  }]);

  if (context.kind === "archive") {
    rows.push([
      { text: t(language, "archiveAll"), callback_data: "archive:filter:all:0" },
      { text: `🟢 ${t(language, "directShort")}`, callback_data: "archive:filter:direct:0" },
      { text: `🟡 ${t(language, "potentialShort")}`, callback_data: "archive:filter:potential:0" },
    ]);
  }

  const previous = context.page > 0
    ? { text: t(language, "previous"), callback_data: context.kind === "news" ? `news:page:${context.page - 1}` : `archive:page:${context.filter}:${context.page - 1}` }
    : undefined;
  const next = hasNext
    ? { text: t(language, "next"), callback_data: context.kind === "news" ? `news:page:${context.page + 1}` : `archive:page:${context.filter}:${context.page + 1}` }
    : undefined;
  if (previous || next) rows.push([...(previous ? [previous] : []), ...(next ? [next] : [])]);

  return keyboard(...rows);
}

export function publicationDetailKeyboard(
  language: Language,
  officialUrl?: string,
): InlineKeyboardMarkup {
  const rows: InlineKeyboardMarkup["inline_keyboard"] = [];
  if (officialUrl) rows.push([{ text: t(language, "openOfficialSource"), url: officialUrl }]);
  return keyboard(...rows);
}
