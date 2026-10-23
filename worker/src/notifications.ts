import type { Language } from "./database/users.ts";
import { validatedOfficialUrl } from "./bot/publications.ts";
import { notificationMainMenuKeyboard } from "./bot/keyboards/index.ts";
import { t } from "./localization/index.ts";
import type { InlineKeyboardMarkup } from "./types/telegram.ts";

const MAX_EVENT_AGE_MS = 7 * 24 * 60 * 60 * 1000;

export interface NotificationEnv {
  DB: D1Database;
  INGEST_API_TOKEN?: string;
  NOTIFICATION_QUEUE_ENABLED?: string;
  NOTIFICATION_ACTIVATION_CUTOFF?: string;
  NOTIFICATION_DELIVERY_ENABLED?: string;
  NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF?: string;
  NOTIFICATION_CANARY_ENABLED?: string;
  NOTIFICATION_CANARY_ACTIVATION_CUTOFF?: string;
  NOTIFICATION_CANARY_TELEGRAM_ID?: string;
  NOTIFICATION_E2E_ENABLED?: string;
  NOTIFICATION_E2E_TELEGRAM_ID?: string;
  NOTIFICATION_E2E_RUN_ID?: string;
  NOTIFICATION_E2E_EVENT_ID?: string;
}

export interface NotificationTelegramClient {
  sendMessage(chatId: number, text: string, keyboard?: InlineKeyboardMarkup): Promise<void>;
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

/** Queue only events produced by a confirmed incremental ingestion run. */
interface NotificationScope {
  recipientId: number;
  eventId: number;
  runId: string;
}

interface NotificationE2EConfig extends NotificationScope {}

const NOTIFICATION_E2E_PREPARE_CONFIRMATION = "PREPARE_ONE_ALLOWLISTED_NOTIFICATION";
const NOTIFICATION_E2E_DELIVERY_CONFIRMATION = "SEND_ONE_ALLOWLISTED_NOTIFICATION";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;

function notificationE2EConfig(env: NotificationEnv): NotificationE2EConfig | null {
  if (env.NOTIFICATION_E2E_ENABLED !== "true") return null;
  const recipientText = env.NOTIFICATION_E2E_TELEGRAM_ID;
  const eventText = env.NOTIFICATION_E2E_EVENT_ID;
  const runId = env.NOTIFICATION_E2E_RUN_ID;
  if (!recipientText || !/^[1-9]\d{0,19}$/u.test(recipientText)
      || !eventText || !/^[1-9]\d{0,15}$/u.test(eventText)
      || !runId || !UUID_PATTERN.test(runId)) return null;
  const recipientId = Number(recipientText);
  const eventId = Number(eventText);
  if (!Number.isSafeInteger(recipientId) || !Number.isSafeInteger(eventId)) return null;
  return { recipientId, eventId, runId };
}

async function prepareNotificationQueueScoped(
  env: NotificationEnv,
  runId: string,
  now: Date,
  scope?: NotificationScope,
): Promise<NotificationQueueResult> {
  if (env.NOTIFICATION_QUEUE_ENABLED !== "true") return { eligible: false, queued: 0 };
  const cutoff = activationCutoff(env.NOTIFICATION_ACTIVATION_CUTOFF);
  if (!cutoff || now.getTime() < cutoff.getTime() || !UUID_PATTERN.test(runId)) {
    return { eligible: false, queued: 0 };
  }
  const run = await env.DB.prepare("SELECT run_id FROM ingestion_runs WHERE run_id = ? AND mode = 'incremental' AND status = 'COMMITTED'")
    .bind(runId).first<{ run_id: string }>();
  if (!run) return { eligible: false, queued: 0 };

  const staleBefore = new Date(now.getTime() - MAX_EVENT_AGE_MS).toISOString();
  const scopeSql = scope ? " AND e.id = ? AND u.telegram_id = ?" : "";
  const scopeBindings = scope ? [scope.eventId, scope.recipientId] : [];
  const result = await env.DB.prepare(`
    INSERT INTO notifications (telegram_id, event_id, status, attempts, created_at)
    SELECT u.telegram_id, e.id, 'PENDING', 0, ?
    FROM publication_events e
    JOIN publications p ON p.id = e.publication_id
    JOIN sources s ON s.id = p.source_id
    JOIN users u ON (p.relevance = 'DIRECT' AND u.notify_direct = 1)
      OR (p.relevance = 'POTENTIAL' AND u.notify_potential = 1)
    WHERE e.event_key LIKE ?
      AND e.notify_eligible = 1
      AND p.historical = 0
      AND p.source_complete = 1
      AND p.source_deleted = 0
      AND p.relevance IN ('DIRECT', 'POTENTIAL')
      AND e.detected_at > ?
      AND e.detected_at >= ?
      AND NOT (s.code = 'hoeringsportalen' AND e.event_type = 'FIRST_SEEN')
      AND NOT EXISTS (
        SELECT 1 FROM notifications n WHERE n.telegram_id = u.telegram_id AND n.event_id = e.id
      )${scopeSql}
    ON CONFLICT (telegram_id, event_id) DO NOTHING
  `).bind(now.toISOString(), `ingest:${runId}:%`, cutoff.toISOString(), staleBefore, ...scopeBindings).run();

  return { eligible: true, queued: result.meta.changes ?? 0 };
}

export async function prepareNotificationQueue(
  env: NotificationEnv,
  runId: string,
  now = new Date(),
): Promise<NotificationQueueResult> {
  return prepareNotificationQueueScoped(env, runId, now);
}

export async function prepareSingleNotificationE2E(
  env: NotificationEnv,
  confirmation: unknown,
  now = new Date(),
): Promise<{ status: "prepared" | "blocked"; queued: 0 | 1 }> {
  const config = notificationE2EConfig(env);
  if (!config || confirmation !== NOTIFICATION_E2E_PREPARE_CONFIRMATION) return { status: "blocked", queued: 0 };
  const result = await prepareNotificationQueueScoped(env, config.runId, now, config);
  if (result.eligible && result.queued === 1) return { status: "prepared", queued: 1 };
  return { status: "blocked", queued: 0 };
}

export interface NotificationPreview {
  activation_configured: boolean;
  eligible_events: number;
  estimated_recipients: number;
  direct_events: number;
  potential_events: number;
  direct_recipients: number;
  potential_recipients: number;
  suppressed_historical_events: number;
  suppressed_stale_events: number;
}

/** Aggregate-only read path: never queues records or exposes account identifiers. */
export async function previewNotifications(env: NotificationEnv, now = new Date()): Promise<NotificationPreview> {
  const cutoff = activationCutoff(env.NOTIFICATION_ACTIVATION_CUTOFF);
  const activationActive = Boolean(cutoff && now.getTime() >= cutoff.getTime());
  const staleBefore = new Date(now.getTime() - MAX_EVENT_AGE_MS).toISOString();
  const cutoffValue = cutoff?.toISOString() ?? "";
  const row = await env.DB.prepare(`
    WITH candidates AS (
      SELECT p.relevance, p.historical, p.source_complete, p.source_deleted, e.notify_eligible,
        e.detected_at, e.event_type, s.code AS source_code,
        (SELECT COUNT(*) FROM users u WHERE
          (p.relevance = 'DIRECT' AND u.notify_direct = 1) OR
          (p.relevance = 'POTENTIAL' AND u.notify_potential = 1)) AS recipient_count
      FROM publication_events e
      JOIN publications p ON p.id = e.publication_id
      JOIN sources s ON s.id = p.source_id
    )
    SELECT
      COALESCE(SUM(CASE WHEN historical = 1 THEN 1 ELSE 0 END), 0) AS suppressed_historical_events,
      COALESCE(SUM(CASE WHEN historical = 0 AND notify_eligible = 1 AND source_complete = 1 AND source_deleted = 0
        AND relevance IN ('DIRECT', 'POTENTIAL') AND detected_at < ? THEN 1 ELSE 0 END), 0) AS suppressed_stale_events,
      COALESCE(SUM(CASE WHEN ? = 1 AND notify_eligible = 1 AND historical = 0 AND source_complete = 1 AND source_deleted = 0
        AND relevance IN ('DIRECT', 'POTENTIAL') AND detected_at > ? AND detected_at >= ?
        AND NOT (source_code = 'hoeringsportalen' AND event_type = 'FIRST_SEEN') THEN 1 ELSE 0 END), 0) AS eligible_events,
      COALESCE(SUM(CASE WHEN ? = 1 AND notify_eligible = 1 AND historical = 0 AND source_complete = 1 AND source_deleted = 0
        AND relevance IN ('DIRECT', 'POTENTIAL') AND detected_at > ? AND detected_at >= ?
        AND NOT (source_code = 'hoeringsportalen' AND event_type = 'FIRST_SEEN') THEN recipient_count ELSE 0 END), 0) AS estimated_recipients,
      COALESCE(SUM(CASE WHEN ? = 1 AND notify_eligible = 1 AND historical = 0 AND source_complete = 1 AND source_deleted = 0
        AND relevance = 'DIRECT' AND detected_at > ? AND detected_at >= ?
        AND NOT (source_code = 'hoeringsportalen' AND event_type = 'FIRST_SEEN') THEN 1 ELSE 0 END), 0) AS direct_events,
      COALESCE(SUM(CASE WHEN ? = 1 AND notify_eligible = 1 AND historical = 0 AND source_complete = 1 AND source_deleted = 0
        AND relevance = 'POTENTIAL' AND detected_at > ? AND detected_at >= ?
        AND NOT (source_code = 'hoeringsportalen' AND event_type = 'FIRST_SEEN') THEN 1 ELSE 0 END), 0) AS potential_events,
      COALESCE(SUM(CASE WHEN ? = 1 AND notify_eligible = 1 AND historical = 0 AND source_complete = 1 AND source_deleted = 0
        AND relevance = 'DIRECT' AND detected_at > ? AND detected_at >= ?
        AND NOT (source_code = 'hoeringsportalen' AND event_type = 'FIRST_SEEN') THEN recipient_count ELSE 0 END), 0) AS direct_recipients,
      COALESCE(SUM(CASE WHEN ? = 1 AND notify_eligible = 1 AND historical = 0 AND source_complete = 1 AND source_deleted = 0
        AND relevance = 'POTENTIAL' AND detected_at > ? AND detected_at >= ?
        AND NOT (source_code = 'hoeringsportalen' AND event_type = 'FIRST_SEEN') THEN recipient_count ELSE 0 END), 0) AS potential_recipients
    FROM candidates
  `).bind(
    staleBefore,
    activationActive ? 1 : 0, cutoffValue, staleBefore,
    activationActive ? 1 : 0, cutoffValue, staleBefore,
    activationActive ? 1 : 0, cutoffValue, staleBefore,
    activationActive ? 1 : 0, cutoffValue, staleBefore,
    activationActive ? 1 : 0, cutoffValue, staleBefore,
    activationActive ? 1 : 0, cutoffValue, staleBefore,
  ).first<Omit<NotificationPreview, "activation_configured">>();
  return {
    activation_configured: activationActive,
    eligible_events: row?.eligible_events ?? 0,
    estimated_recipients: row?.estimated_recipients ?? 0,
    direct_events: row?.direct_events ?? 0,
    potential_events: row?.potential_events ?? 0,
    direct_recipients: row?.direct_recipients ?? 0,
    potential_recipients: row?.potential_recipients ?? 0,
    suppressed_historical_events: row?.suppressed_historical_events ?? 0,
    suppressed_stale_events: row?.suppressed_stale_events ?? 0,
  };
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

export interface CanaryRequest {
  confirmation?: unknown;
  request_id?: unknown;
  recipient_id?: unknown;
}

export type CanaryResult = { status: "blocked" | "already_used" | "sent" | "failed" };

const CANARY_MESSAGE = "🧪 DenmarkBot — тестовое уведомление";
const CANARY_CONFIRMATION = "SEND_ONE_CANARY_MESSAGE";

/**
 * Send one fixed diagnostic message to one explicitly allowlisted account.
 * The singleton D1 claim makes every attempt terminal, including ambiguous sends.
 */
export async function sendNotificationCanary(
  env: NotificationEnv,
  telegram: NotificationTelegramClient,
  request: CanaryRequest,
  now = new Date(),
): Promise<CanaryResult> {
  const rawRecipient = env.NOTIFICATION_CANARY_TELEGRAM_ID;
  if (env.NOTIFICATION_CANARY_ENABLED !== "true" || !rawRecipient || !/^[1-9]\d{0,19}$/u.test(rawRecipient)) return { status: "blocked" };
  const recipientId = Number(rawRecipient);
  if (!Number.isSafeInteger(recipientId) || recipientId <= 0 || request.recipient_id !== recipientId) return { status: "blocked" };
  if (request.confirmation !== CANARY_CONFIRMATION || typeof request.request_id !== "string" || !/^[0-9a-f-]{36}$/iu.test(request.request_id)) return { status: "blocked" };
  const cutoff = activationCutoff(env.NOTIFICATION_CANARY_ACTIVATION_CUTOFF);
  if (!cutoff || now.getTime() < cutoff.getTime()) return { status: "blocked" };

  const claim = await env.DB.prepare(`
    INSERT INTO notification_canary_attempts (singleton_id, telegram_id, request_id, status, created_at)
    VALUES (1, ?, ?, 'SENDING', ?)
    ON CONFLICT (singleton_id) DO NOTHING
  `).bind(recipientId, request.request_id, now.toISOString()).run();
  if ((claim.meta.changes ?? 0) !== 1) return { status: "already_used" };

  try {
    await telegram.sendMessage(recipientId, CANARY_MESSAGE);
  } catch {
    await env.DB.prepare("UPDATE notification_canary_attempts SET status = 'FAILED', completed_at = ?, last_error = 'telegram_delivery_ambiguous_or_failed' WHERE singleton_id = 1 AND status = 'SENDING'")
      .bind(now.toISOString()).run();
    return { status: "failed" };
  }
  const finalized = await env.DB.prepare("UPDATE notification_canary_attempts SET status = 'SENT', completed_at = ?, last_error = NULL WHERE singleton_id = 1 AND status = 'SENDING'")
    .bind(now.toISOString()).run();
  return (finalized.meta.changes ?? 0) === 1 ? { status: "sent" } : { status: "failed" };
}

/** One attempt only. A timeout is ambiguous, so failures are terminal to avoid duplicate sends. */
export async function deliverPendingNotifications(
  env: NotificationEnv,
  telegram: NotificationTelegramClient,
  now = new Date(),
  scope?: NotificationScope,
): Promise<DeliveryResult> {
  if (env.NOTIFICATION_DELIVERY_ENABLED !== "true") return { enabled: false, sent: 0, failed: 0 };
  const cutoff = activationCutoff(env.NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF);
  if (!cutoff || now.getTime() < cutoff.getTime()) return { enabled: false, sent: 0, failed: 0 };
  const staleBefore = new Date(now.getTime() - MAX_EVENT_AGE_MS).toISOString();
  const scopeSql = scope ? " AND n.telegram_id = ? AND e.id = ? AND e.event_key LIKE ?" : "";
  const scopeBindings = scope ? [scope.recipientId, scope.eventId, `ingest:${scope.runId}:%`] : [];
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
      AND p.source_deleted = 0
      AND p.relevance IN ('DIRECT', 'POTENTIAL')
      AND ((p.relevance = 'DIRECT' AND u.notify_direct = 1)
        OR (p.relevance = 'POTENTIAL' AND u.notify_potential = 1))
      AND e.detected_at > ?
      AND e.detected_at >= ?
      AND NOT (s.code = 'hoeringsportalen' AND e.event_type = 'FIRST_SEEN')${scopeSql}
    ORDER BY n.created_at, n.id
    LIMIT ${scope ? 1 : 100}
  `).bind(cutoff.toISOString(), staleBefore, ...scopeBindings).all<PendingNotification>();

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
      await telegram.sendMessage(row.telegram_id, text, notificationMainMenuKeyboard(row.language));
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


export async function deliverSingleNotificationE2E(
  env: NotificationEnv,
  telegram: NotificationTelegramClient,
  confirmation: unknown,
  now = new Date(),
): Promise<{ status: "sent" | "failed" | "blocked"; sent: 0 | 1; failed: 0 | 1 }> {
  const config = notificationE2EConfig(env);
  if (!config || confirmation !== NOTIFICATION_E2E_DELIVERY_CONFIRMATION) {
    return { status: "blocked", sent: 0, failed: 0 };
  }
  const result = await deliverPendingNotifications(env, telegram, now, config);
  if (result.sent === 1 && result.failed === 0) return { status: "sent", sent: 1, failed: 0 };
  if (result.sent === 0 && result.failed === 1) return { status: "failed", sent: 0, failed: 1 };
  return { status: "blocked", sent: 0, failed: 0 };
}
