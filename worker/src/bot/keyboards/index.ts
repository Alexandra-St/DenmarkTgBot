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

export function languageKeyboard(language: Language): InlineKeyboardMarkup {
  return keyboard(
    [{ text: "Українська", callback_data: "lang:uk" }],
    [{ text: "Русский", callback_data: "lang:ru" }],
    [{ text: t(language, "main"), callback_data: "nav:main" }],
  );
}

export function settingsLanguageKeyboard(language: Language): InlineKeyboardMarkup {
  return keyboard(
    [{ text: "Українська", callback_data: "lang:uk" }],
    [{ text: "Русский", callback_data: "lang:ru" }],
    [{ text: t(language, "back"), callback_data: "nav:settings" }],
    [{ text: t(language, "main"), callback_data: "nav:main" }],
  );
}

export function mainKeyboard(language: Language): InlineKeyboardMarkup {
  return keyboard(
    [{ text: t(language, "news"), callback_data: "menu:news" }],
    [{ text: t(language, "archive"), callback_data: "menu:archive" }],
    [{ text: t(language, "settings"), callback_data: "menu:settings" }],
    [{ text: t(language, "about"), callback_data: "menu:about" }],
  );
}

export function persistentReplyKeyboard(language: Language): ReplyKeyboardMarkup {
  return {
    keyboard: [
      [{ text: t(language, "news") }, { text: t(language, "archive") }],
      [{ text: t(language, "settings") }, { text: t(language, "about") }],
    ],
    is_persistent: true,
    resize_keyboard: true,
  };
}

export function settingsKeyboard(language: Language): InlineKeyboardMarkup {
  return keyboard(
    [{ text: t(language, "language"), callback_data: "settings:language" }],
    [{ text: t(language, "notifications"), callback_data: "settings:notifications" }],
    [{ text: t(language, "main"), callback_data: "nav:main" }],
  );
}

export function notificationKeyboard(
  language: Language,
  direct: boolean,
  potential: boolean,
): InlineKeyboardMarkup {
  const directAction = direct ? "disableDirect" : "enableDirect";
  const potentialAction = potential ? "disablePotential" : "enablePotential";
  return keyboard(
    [{
      text: `${direct ? "✅" : "⬜"} ${t(language, directAction)}`,
      callback_data: `notify:direct:set:${direct ? 0 : 1}`,
    }],
    [{
      text: `${potential ? "✅" : "⬜"} ${t(language, potentialAction)}`,
      callback_data: `notify:potential:set:${potential ? 0 : 1}`,
    }],
    [{ text: t(language, "back"), callback_data: "nav:settings" }],
  );
}

export function backToMainKeyboard(language: Language): InlineKeyboardMarkup {
  return keyboard([{ text: t(language, "main"), callback_data: "nav:main" }]);
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

  if (context.kind === "news") {
    rows.push([{ text: t(language, "archive"), callback_data: "menu:archive" }]);
  } else {
    rows.push([{
      text: t(language, "back"),
      callback_data: context.filter === "all" ? "menu:news" : "archive:filter:all:0",
    }]);
  }
  rows.push([{ text: t(language, "main"), callback_data: "nav:main" }]);
  return keyboard(...rows);
}

export function publicationDetailKeyboard(
  language: Language,
  context: PublicationListContext,
  officialUrl?: string,
): InlineKeyboardMarkup {
  const rows: InlineKeyboardMarkup["inline_keyboard"] = [];
  if (officialUrl) rows.push([{ text: t(language, "openOfficialSource"), url: officialUrl }]);
  rows.push([{
    text: t(language, "back"),
    callback_data: context.kind === "news"
      ? `news:page:${context.page}`
      : `archive:page:${context.filter}:${context.page}`,
  }]);
  rows.push([{ text: t(language, "main"), callback_data: "nav:main" }]);
  return keyboard(...rows);
}
