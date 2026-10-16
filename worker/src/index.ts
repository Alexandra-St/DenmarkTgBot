import { handleTelegramUpdate } from "./bot/handlers/webhook.ts";
import { TelegramApi } from "./bot/telegram.ts";
import { D1UserRepository } from "./database/users.ts";
import { D1PublicationRepository } from "./database/publications.ts";
import type { TelegramUpdate } from "./types/telegram.ts";
import { handleIngestionRequest, handleSourceStatesRequest, isIngestionRequestAuthorized } from "./ingestion.ts";
import { deliverPendingNotifications, prepareNotificationQueue, previewNotifications, sendNotificationCanary, type NotificationEnv } from "./notifications.ts";

interface Env {
  DB: D1Database;
  TELEGRAM_BOT_TOKEN?: string;
  TELEGRAM_WEBHOOK_SECRET?: string;
  INGEST_API_TOKEN?: string;
  NOTIFICATION_QUEUE_ENABLED?: string;
  NOTIFICATION_ACTIVATION_CUTOFF?: string;
  NOTIFICATION_DELIVERY_ENABLED?: string;
  NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF?: string;
  NOTIFICATION_CANARY_ENABLED?: string;
  NOTIFICATION_CANARY_ACTIVATION_CUTOFF?: string;
  NOTIFICATION_CANARY_TELEGRAM_ID?: string;
}

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });

function secretsMatch(expected: string, actual: string): boolean {
  if (expected.length === 0 || expected.length !== actual.length) return false;
  let mismatch = 0;
  for (let index = 0; index < expected.length; index += 1) {
    mismatch |= expected.charCodeAt(index) ^ actual.charCodeAt(index);
  }
  return mismatch === 0;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health" && request.method === "GET") {
      return json({ status: "ok", service: "DenmarkTgBot" });
    }

    if (url.pathname === "/internal/ingest") return handleIngestionRequest(request, env);
    if (url.pathname === "/internal/source-states") return handleSourceStatesRequest(request, env);
    if (url.pathname.startsWith("/internal/notifications/")) {
      if (!await isIngestionRequestAuthorized(request, env)) return json({ error: "unauthorized" }, 401);
      const notificationEnv = env as Env & NotificationEnv;
      if (url.pathname === "/internal/notifications/preview") {
        if (request.method !== "GET") return json({ error: "method_not_allowed" }, 405);
        return json(await previewNotifications(notificationEnv));
      }
      if (url.pathname === "/internal/notifications/prepare") {
        if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
        let body: unknown;
        try { body = await request.json(); } catch { return json({ error: "invalid_json" }, 400); }
        if (!body || typeof body !== "object" || typeof (body as { run_id?: unknown }).run_id !== "string") {
          return json({ error: "invalid_run_id" }, 400);
        }
        return json(await prepareNotificationQueue(notificationEnv, (body as { run_id: string }).run_id));
      }
      if (url.pathname === "/internal/notifications/deliver") {
        if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
        if (env.NOTIFICATION_DELIVERY_ENABLED !== "true" || !env.NOTIFICATION_DELIVERY_ACTIVATION_CUTOFF) {
          return json({ enabled: false, sent: 0, failed: 0 });
        }
        if (!env.TELEGRAM_BOT_TOKEN) return json({ error: "delivery_not_configured" }, 503);
        const result = await deliverPendingNotifications(notificationEnv, new TelegramApi(env.TELEGRAM_BOT_TOKEN));
        return json(result);
      }
      if (url.pathname === "/internal/notifications/canary") {
        if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
        let body: unknown;
        try { body = await request.json(); } catch { return json({ error: "invalid_json" }, 400); }
        if (!body || typeof body !== "object" || Array.isArray(body)) return json({ error: "invalid_canary_request" }, 400);
        // Do not consume the single-use attempt when Telegram is not configured.
        if (!env.TELEGRAM_BOT_TOKEN) return json({ status: "blocked" });
        return json(await sendNotificationCanary(notificationEnv, new TelegramApi(env.TELEGRAM_BOT_TOKEN), body as {
          confirmation?: unknown; request_id?: unknown; recipient_id?: unknown;
        }));
      }
      return json({ error: "Not Found" }, 404);
    }

    if (url.pathname !== "/telegram/webhook") return json({ error: "Not Found" }, 404);
    if (request.method !== "POST") return json({ error: "Method Not Allowed" }, 405);
    if (!env.TELEGRAM_WEBHOOK_SECRET) {
      return json({ error: "Webhook is not configured" }, 503);
    }

    const providedSecret = request.headers.get("X-Telegram-Bot-Api-Secret-Token") ?? "";
    if (!secretsMatch(env.TELEGRAM_WEBHOOK_SECRET, providedSecret)) {
      return json({ error: "Unauthorized" }, 401);
    }
    if (!env.TELEGRAM_BOT_TOKEN) {
      return json({ error: "Telegram bot is not configured" }, 503);
    }

    let update: TelegramUpdate;
    try {
      update = (await request.json()) as TelegramUpdate;
      if (!update || typeof update !== "object" || Array.isArray(update)) {
        return json({ error: "Invalid update" }, 400);
      }
    } catch {
      return json({ error: "Malformed JSON" }, 400);
    }

    try {
      const repository = new D1UserRepository(env.DB);
      const publications = new D1PublicationRepository(env.DB);
      const telegram = new TelegramApi(env.TELEGRAM_BOT_TOKEN);
      await handleTelegramUpdate(update, repository, telegram, publications);
      return new Response(null, { status: 200 });
    } catch {
      // Do not log update contents or Telegram credentials; Telegram can retry a 5xx.
      return json({ error: "Unable to process update" }, 500);
    }
  },
} satisfies ExportedHandler<Env>;
