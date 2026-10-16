import type { Language } from "../../database/users.ts";
import { t } from "../../localization/index.ts";
import type { InlineKeyboardMarkup } from "../../types/telegram.ts";

const keyboard = (...rows: InlineKeyboardMarkup["inline_keyboard"]): InlineKeyboardMarkup => ({
  inline_keyboard: rows,
});

export function languageKeyboard(language: Language): InlineKeyboardMarkup {
  return keyboard(
    [{ text: "🇺🇦 Українська", callback_data: "lang:uk" }],
    [{ text: "🇷🇺 Русский", callback_data: "lang:ru" }],
    [{ text: t(language, "main"), callback_data: "nav:main" }],
  );
}

export function settingsLanguageKeyboard(language: Language): InlineKeyboardMarkup {
  return keyboard(
    [{ text: "🇺🇦 Українська", callback_data: "lang:uk" }],
    [{ text: "🇷🇺 Русский", callback_data: "lang:ru" }],
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
