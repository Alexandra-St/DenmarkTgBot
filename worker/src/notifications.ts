import type { Language } from "./database/users.ts";
import { validatedOfficialUrl } from "./bot/publications.ts";
import { t } from "./localization/index.ts";

const MAX_EVENT_AGE_MS = 7 * 24 * 60 * 60 * 1000;

export interface NotificationEnv {
  DB: D1Database;
  NOTIFICATION_ACTIVATION_CUTOFF?: string;
  NOTIFICATION_DELIVERY_ENABLED?: string;
}

export interface NotificationTelegramClient {
  sendMessage(chatId: number, text: string): Promise<void>;
}

export interface NotificationQueueResult {
  eligible: boolean;
  queued: number;
}

function activationCutoff(value: string | undefined): Date | null {
  if (!value || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?Z$/.test(value)) return null;
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 19) !== value.slice(0, 19)) return null;
  const fraction = /\.(\d{1,3})Z$/.exec(value)?.[1];
  if (fraction && parsed.toISOString().slice(20, 23) !== fraction.padEnd(3, "0")) return null;
  return parsed;
}

/** Create durable pending rows only after an explicit activation cutoff is configured and reached. */
export async function prepareNotificationQueue(
  env: NotificationEnv,
  now = new Date(),
): Promise<NotificationQueueResult> {
  const cutoff = activationCutoff(env.NOTIFICATION_ACTIVATION_CUTOFF);
  if (!cutoff || now.getTime() < cutoff.getTime()) return { eligible: false, queued: 0 };

  const staleBefore = new Date(now.getTime() - MAX_EVENT_AGE_MS).toISOString();
  const result = await env.DB.prepare(`
    INSERT INTO notifications (telegram_id, event_id, status, attempts, created_at)
    SELECT u.telegram_id, e.id, 'PENDING', 0, ?
    FROM publication_events e
    JOIN publications p ON p.id = e.publication_id
    JOIN sources s ON s.id = p.source_id
    JOIN users u ON (p.relevance = 'DIRECT' AND u.notify_direct = 1)
      OR (p.relevance = 'POTENTIAL' AND u.notify_potential = 1)
    WHERE e.notify_eligible = 1
      AND p.historical = 0
      AND p.source_complete = 1
      AND p.relevance IN ('DIRECT', 'POTENTIAL')
      AND e.detected_at > ?
      AND e.detected_at >= ?
      AND NOT (s.code = 'hoeringsportalen' AND e.event_type = 'FIRST_SEEN')
      AND NOT EXISTS (
        SELECT 1 FROM notifications n WHERE n.telegram_id = u.telegram_id AND n.event_id = e.id
      )
    ON CONFLICT (telegram_id, event_id) DO NOTHING
  `).bind(now.toISOString(), cutoff.toISOString(), staleBefore).run();

  return { eligible: true, queued: result.meta.changes ?? 0 };
}

interface PendingNotification {
  id: number;
  telegram_id: number;
  language: Language;
  event_type: string;
  previous_status: string | null;
  new_status: string | null;
  relevance: "DIRECT" | "POTENTIAL";
  title: string;
  source_code: string;
  source_name: string;
  external_url: string;
}

function clean(value: string, max: number): string {
  return Array.from(value.replace(/[\u0000-\u001f\u007f]/gu, " ").replace(/\s+/gu, " ").trim())
    .slice(0, max).join("");
}

function eventLabel(event: PendingNotification, language: Language): string | null {
  const keys: Record<string, Parameters<typeof t>[1]> = {
    FIRST_SEEN: "notificationFirstSeen",
    TITLE_CHANGED: "notificationTitleChanged",
    CONTENT_CHANGED: "notificationContentChanged",
    STATUS_CHANGED: "notificationStatusChanged",
    PUBLICATION_TYPE_CHANGED: "notificationTypeChanged",
    DOCUMENTS_CHANGED: "notificationDocumentsChanged",
    STAGES_CHANGED: "notificationStagesChanged",
    VOTES_CHANGED: "notificationVotesChanged",
    RECLASSIFIED: "notificationReclassified",
  };
  const key = keys[event.event_type];
  if (!key) return null;
  const lines = [t(language, key)];
  if (event.event_type === "STATUS_CHANGED") {
    if (event.previous_status) lines.push(t(language, "notificationPreviousStatus") + ": " + clean(event.previous_status, 200));
    if (event.new_status) lines.push(t(language, "notificationNewStatus") + ": " + clean(event.new_status, 200));
  }
  return lines.join("\n");
}

export function formatNotification(event: PendingNotification): string | null {
  const url = validatedOfficialUrl(event.source_code, event.external_url);
  const change = eventLabel(event, event.language);
  if (!url || !change) return null;
  const marker = event.relevance === "DIRECT" ? "🟢" : "🟡";
  const disclaimer = event.language === "uk"
    ? "Інформаційне повідомлення, не юридична консультація."
    : "Информационное сообщение, не юридическая консультация.";
  return [
    marker + " " + t(event.language, event.relevance === "DIRECT" ? "directShort" : "potentialShort"),
    t(event.language, "notificationChangeTitle"),
    change,
    clean(event.title, 500),
    t(event.language, "sourceLabel") + ": " + clean(event.source_name, 120),
    url,
    disclaimer,
  ].join("\n\n");
}

export interface DeliveryResult {
  enabled: boolean;
  sent: number;
  failed: number;
}

/** One attempt only. A timeout is ambiguous, so failures are terminal to avoid duplicate sends. */
export async function deliverPendingNotifications(
  env: NotificationEnv,
  telegram: NotificationTelegramClient,
  now = new Date(),
): Promise<DeliveryResult> {
  if (env.NOTIFICATION_DELIVERY_ENABLED !== "true") return { enabled: false, sent: 0, failed: 0 };
  const cutoff = activationCutoff(env.NOTIFICATION_ACTIVATION_CUTOFF);
  if (!cutoff || now.getTime() < cutoff.getTime()) return { enabled: false, sent: 0, failed: 0 };
  const staleBefore = new Date(now.getTime() - MAX_EVENT_AGE_MS).toISOString();
  const rows = await env.DB.prepare(`
    SELECT n.id, n.telegram_id, u.language, e.event_type, e.previous_status, e.new_status,
      u.notify_direct, u.notify_potential, p.historical, p.source_complete,
      p.relevance, p.original_title AS title, s.code AS source_code, s.name AS source_name,
      p.external_url
    FROM notifications n
    JOIN users u ON u.telegram_id = n.telegram_id
    JOIN publication_events e ON e.id = n.event_id
    JOIN publications p ON p.id = e.publication_id
    JOIN sources s ON s.id = p.source_id
    WHERE n.status = 'PENDING'
      AND e.notify_eligible = 1
      AND p.historical = 0
      AND p.source_complete = 1
      AND p.relevance IN ('DIRECT', 'POTENTIAL')
      AND ((p.relevance = 'DIRECT' AND u.notify_direct = 1)
        OR (p.relevance = 'POTENTIAL' AND u.notify_potential = 1))
      AND e.detected_at > ?
      AND e.detected_at >= ?
      AND NOT (s.code = 'hoeringsportalen' AND e.event_type = 'FIRST_SEEN')
    ORDER BY n.created_at, n.id
    LIMIT 100
  `).bind(cutoff.toISOString(), staleBefore).all<PendingNotification>();

  let sent = 0;
  let failed = 0;
  for (const row of rows.results ?? []) {
    const claim = await env.DB.prepare("UPDATE notifications SET status = 'SENDING' WHERE id = ? AND status = 'PENDING'")
      .bind(row.id).run();
    if ((claim.meta.changes ?? 0) !== 1) continue;
    const text = formatNotification(row);
    if (!text) {
      await env.DB.prepare("UPDATE notifications SET status = 'FAILED', last_error = 'invalid_notification_content' WHERE id = ? AND status = 'SENDING'")
        .bind(row.id).run();
      failed += 1;
      continue;
    }
    try {
      await telegram.sendMessage(row.telegram_id, text);
      const result = await env.DB.prepare("UPDATE notifications SET status = 'SENT', attempts = attempts + 1, sent_at = ?, last_error = NULL WHERE id = ? AND status = 'SENDING'")
        .bind(now.toISOString(), row.id).run();
      if ((result.meta.changes ?? 0) === 1) sent += 1;
    } catch {
      // Do not retry automatically: Telegram may have accepted a message before a timeout.
      await env.DB.prepare("UPDATE notifications SET status = 'FAILED', attempts = attempts + 1, last_error = 'telegram_delivery_ambiguous_or_failed' WHERE id = ? AND status = 'SENDING'")
        .bind(row.id).run();
      failed += 1;
    }
  }
  return { enabled: true, sent, failed };
}
