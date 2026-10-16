import type { Language } from "../database/users.ts";
import { t } from "../localization/index.ts";

export type SourceFreshness = "active" | "stale" | "unknown";
export const SOURCE_STALE_AFTER_MS = 24 * 60 * 60 * 1000;
const SOURCE_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i;

export function sourceFreshness(timestamp: string | null | undefined, now = Date.now()): SourceFreshness {
  if (!timestamp) return "unknown";
  if (!SOURCE_TIMESTAMP.test(timestamp)) return "unknown";
  const parsed = Date.parse(timestamp);
  if (!Number.isFinite(parsed) || parsed > now + 5 * 60 * 1000) return "unknown";
  return now - parsed <= SOURCE_STALE_AFTER_MS ? "active" : "stale";
}

export function sourceStatusScreen(language: Language, timestamp: string | null, now = Date.now()): string {
  const freshness = sourceFreshness(timestamp, now);
  let hearingStatus: string;
  if (freshness === "unknown") {
    hearingStatus = t(language, "sourceStatusUnknown");
  } else {
    const formatted = new Intl.DateTimeFormat(language === "uk" ? "uk-UA" : "ru-RU", {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: "Europe/Copenhagen",
    }).format(new Date(timestamp!));
    const key = freshness === "active" ? "sourceStatusActive" : "sourceStatusStale";
    hearingStatus = t(language, key).replace("{date}", formatted);
  }

  return [
    t(language, "sourceStatusTitle"),
    `${t(language, "sourceHoeringsportalen")}: ${hearingStatus}`,
    `${t(language, "sourceFolketinget")}: ${t(language, "sourceMonitoringInactive")}`,
    t(language, "sourceStatusDisclaimer"),
  ].join("\n");
}
