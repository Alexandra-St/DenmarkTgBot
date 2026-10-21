import type { Language } from "../database/users.ts";
import type {
  ArchiveFilter,
  PublicationRecord,
  PublicationRepository,
} from "../database/publications.ts";
import { t } from "../localization/index.ts";
import type { InlineKeyboardMarkup } from "../types/telegram.ts";
import {
  publicationDetailKeyboard,
  publicationListKeyboard,
  type PublicationListContext,
} from "./keyboards/index.ts";

const SOURCE_HOSTS: Record<string, ReadonlySet<string>> = {
  folketinget: new Set(["oda.ft.dk", "ft.dk", "www.ft.dk"]),
  hoeringsportalen: new Set(["hoeringsportalen.dk", "www.hoeringsportalen.dk"]),
};

function clip(value: string, maxCodePoints: number): string {
  const chars = Array.from(value);
  return chars.length > maxCodePoints ? `${chars.slice(0, maxCodePoints - 1).join("")}…` : value;
}

function plain(value: string): string {
  return value.replace(/[\u0000-\u001f\u007f]/gu, " ").replace(/\s+/gu, " ").trim();
}

export function validatedOfficialUrl(sourceCode: string, value: string): string | null {
  const hosts = SOURCE_HOSTS[sourceCode];
  if (!hosts) return null;
  try {
    const url = new URL(value);
    if (
      url.protocol !== "https:" ||
      !hosts.has(url.hostname.toLowerCase()) ||
      (url.port !== "" && url.port !== "443") ||
      url.username !== "" ||
      url.password !== ""
    ) return null;
    return url.toString();
  } catch {
    return null;
  }
}

export function formatSourceDate(value: string | null, language: Language): string | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/u.test(value)) return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  const locale = language === "uk" ? "uk-UA" : "ru-RU";
  return new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(date);
}

function itemText(item: PublicationRecord, language: Language): string {
  const marker = item.relevance === "DIRECT" ? "🟢" : "🟡";
  const title = clip(plain(item.original_title), 420);
  const lines = [`${marker} ${title}`, `${t(language, "sourceLabel")}: ${clip(plain(item.source_name), 120)}`];
  const date = formatSourceDate(item.source_updated_at, language);
  if (date) lines.push(`${t(language, "sourceUpdatedLabel")}: ${date}`);
  return lines.join("\n");
}

export interface PublicationScreen {
  text: string;
  keyboard: InlineKeyboardMarkup;
}

export async function publicationListScreen(
  repository: PublicationRepository,
  language: Language,
  context: PublicationListContext,
): Promise<PublicationScreen> {
  const page = context.kind === "news"
    ? await repository.listNews(context.page)
    : await repository.listArchive(context.filter, context.page);
  const items = page.items.slice(0, 5);
  const heading = context.kind === "news" ? t(language, "newsHeading") : t(language, "archiveHeading");
  const intro = context.kind === "news" ? t(language, "latestTrackedDescription") : t(language, "archiveDescription");
  const text = items.length
    ? `${heading}\n\n${intro}\n\n${t(language, "publicationListNotice")}\n\n${items.map((item) => itemText(item, language)).join("\n\n")}`
    : `${heading}\n\n${intro}\n\n${context.kind === "news" ? t(language, "newsEmpty") : t(language, "archiveEmpty")}\n\n${t(language, "publicationListNotice")}`;
  return {
    text,
    keyboard: publicationListKeyboard(language, items, page.hasNext, context),
  };
}

export async function publicationDetailScreen(
  repository: PublicationRepository,
  language: Language,
  id: number,
  context: PublicationListContext,
): Promise<PublicationScreen> {
  const item = await repository.findById(id);
  if (!item || (context.kind === "archive" && context.filter !== "all" && item.relevance.toLowerCase() !== context.filter)) {
    const fallbackContext: PublicationListContext = context.kind === "news"
      ? { kind: "news", page: context.page }
      : { kind: "archive", filter: context.filter, page: context.page };
    return {
      text: t(language, "publicationNotFound"),
      keyboard: publicationListKeyboard(language, [], false, fallbackContext),
    };
  }

  const url = validatedOfficialUrl(item.source_code, item.external_url);
  const title = clip(plain(item.original_title), 2500);
  const lines = [
    t(language, "officialSourceInformation"),
    "",
    `${t(language, "titleLabel")}: ${title}`,
    `${t(language, "sourceLabel")}: ${clip(plain(item.source_name), 120)}`,
  ];
  if (item.current_status && plain(item.current_status)) {
    lines.push(`${t(language, "currentStatusLabel")}: ${clip(plain(item.current_status), 400)}`);
  }
  const date = formatSourceDate(item.source_updated_at, language);
  if (date) lines.push(`${t(language, "sourceUpdatedLabel")}: ${date}`);
  lines.push("", `${t(language, "botClassificationLabel")}: ${item.relevance === "DIRECT" ? t(language, "direct") : t(language, "potential")}`, "", t(language, "legalDisclaimer"));

  return {
    text: lines.join("\n"),
    keyboard: publicationDetailKeyboard(language, url ?? undefined),
  };
}

export function parseArchiveFilter(value: string): ArchiveFilter | null {
  return value === "all" || value === "direct" || value === "potential" ? value : null;
}
