import type { InlineKeyboardMarkup } from "../types/telegram.ts";

export interface TelegramClient {
  sendMessage(chatId: number, text: string, keyboard?: InlineKeyboardMarkup): Promise<void>;
  editMessage(chatId: number, messageId: number, text: string, keyboard?: InlineKeyboardMarkup): Promise<void>;
  answerCallback(callbackId: string): Promise<void>;
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
    });
    if (!response.ok) throw new Error(`Telegram API returned ${response.status}`);
    const result = (await response.json()) as { ok?: boolean };
    if (!result.ok) throw new Error("Telegram API rejected the request");
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
    await this.call("editMessageText", {
      chat_id: chatId,
      message_id: messageId,
      text,
      ...(keyboard ? { reply_markup: keyboard } : {}),
    });
  }

  async answerCallback(callbackId: string): Promise<void> {
    await this.call("answerCallbackQuery", { callback_query_id: callbackId });
  }
}
