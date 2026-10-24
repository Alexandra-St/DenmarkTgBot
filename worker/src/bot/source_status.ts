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

export function sourceStatusScreen(
  language: Language,
  hoeringsportalenTimestamp: string | null,
  folketingetTimestamp: string | null,
  now = Date.now(),
): string {
  const statusLabel = (timestamp: string | null): string => {
    const freshness = sourceFreshness(timestamp, now);
    return t(language, freshness === "active" ? "sourceStatusActive"
      : freshness === "stale" ? "sourceStatusStale" : "sourceStatusUnknown");
  };
  return [
    t(language, "sourceStatusTitle"),
    `${t(language, "sourceHoeringsportalen")}: ${statusLabel(hoeringsportalenTimestamp)}`,
    `${t(language, "sourceFolketinget")}: ${statusLabel(folketingetTimestamp)}`,
    `${t(language, "sourcePlannedHeading")}: ${t(language, "sourcePlannedNames")}`,
    t(language, "sourceStatusDisclaimer"),
  ].join("\n");
}
