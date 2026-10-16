import type { Language } from "../database/users.ts";
import { ru } from "./ru.ts";
import { uk, type TranslationKey } from "./uk.ts";

export type { TranslationKey } from "./uk.ts";

export function t(language: Language, key: TranslationKey): string {
  return language === "ru" ? ru[key] : uk[key];
}
