import type { InlineKeyboardMarkup } from "../types/telegram.ts";

export interface TelegramClient {
  sendMessage(chatId: number, text: string, keyboard?: InlineKeyboardMarkup): Promise<void>;
  editMessage(chatId: number, messageId: number, text: string, keyboard?: InlineKeyboardMarkup): Promise<void>;
  answerCallback(callbackId: string): Promise<void>;
}

export class TelegramApiError extends Error {
  readonly errorCode: number;
  readonly description: string;

  constructor(errorCode: number, description: string) {
    super(`Telegram API error ${errorCode}: ${description}`);
    this.name = "TelegramApiError";
    this.errorCode = errorCode;
    this.description = description;
  }
}

export class TelegramApi implements TelegramClient {
  private readonly token: string;

  constructor(token: string) {
    this.token = token;
  }

  private async call(method: string, payload: Record<string, unknown>): Promise<void> {
    if (!this.token) throw new Error("Telegram bot token is not configured");
    const response = await fetch(`https://api.telegram.org/bot${this.token}/${method}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      // Callback answers expire quickly; keep other Telegram writes bounded too.
      // Do not retry writes: an ambiguous timeout could otherwise duplicate them.
      signal: AbortSignal.timeout(method === "answerCallbackQuery" ? 3_000 : 8_000),
    });
    let result: { ok?: boolean; error_code?: number; description?: string };
    try {
      result = (await response.json()) as typeof result;
    } catch {
      if (!response.ok) throw new TelegramApiError(response.status, `HTTP ${response.status}`);
      throw new Error("Telegram API returned an invalid response");
    }
    if (!response.ok || !result.ok) {
      throw new TelegramApiError(
        result.error_code ?? response.status,
        result.description ?? `HTTP ${response.status}`,
      );
    }
  }

  async sendMessage(chatId: number, text: string, keyboard?: InlineKeyboardMarkup): Promise<void> {
    await this.call("sendMessage", {
      chat_id: chatId,
      text,
      ...(keyboard ? { reply_markup: keyboard } : {}),
    });
  }

  async editMessage(
    chatId: number,
    messageId: number,
    text: string,
    keyboard?: InlineKeyboardMarkup,
  ): Promise<void> {
    try {
      await this.call("editMessageText", {
        chat_id: chatId,
        message_id: messageId,
        text,
        ...(keyboard ? { reply_markup: keyboard } : {}),
      });
    } catch (error) {
      if (
        error instanceof TelegramApiError &&
        error.errorCode === 400 &&
        error.description.toLowerCase() === "bad request: message is not modified"
      ) return;
      throw error;
    }
  }

  async answerCallback(callbackId: string): Promise<void> {
    try {
      await this.call("answerCallbackQuery", { callback_query_id: callbackId });
    } catch (error) {
      if (
        error instanceof TelegramApiError &&
        error.errorCode === 400 &&
        error.description.toLowerCase() ===
          "bad request: query is too old and response timeout expired or query id is invalid"
      ) return;
      throw error;
    }
  }
}
