import { handleTelegramUpdate } from "./bot/handlers/webhook.ts";
import { TelegramApi } from "./bot/telegram.ts";
import { D1UserRepository } from "./database/users.ts";
import { D1PublicationRepository } from "./database/publications.ts";
import type { TelegramUpdate } from "./types/telegram.ts";
import { handleIngestionRequest, handleSourceStatesRequest } from "./ingestion.ts";

interface Env {
  DB: D1Database;
  TELEGRAM_BOT_TOKEN?: string;
  TELEGRAM_WEBHOOK_SECRET?: string;
  INGEST_API_TOKEN?: string;
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
