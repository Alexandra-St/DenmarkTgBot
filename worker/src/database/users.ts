export type Language = "uk" | "ru";

export interface UserPreferences {
  telegram_id: number;
  language: Language;
  notify_direct: number;
  notify_potential: number;
}

export interface UserRepository {
  find(telegramId: number): Promise<UserPreferences | null>;
  create(telegramId: number, language: Language): Promise<void>;
  updateLanguage(telegramId: number, language: Language): Promise<void>;
  updateNotification(telegramId: number, preference: "direct" | "potential", enabled: boolean): Promise<void>;
}

export class D1UserRepository implements UserRepository {
  private readonly db: D1Database;

  constructor(db: D1Database) {
    this.db = db;
  }

  async find(telegramId: number): Promise<UserPreferences | null> {
    return this.db
      .prepare("SELECT telegram_id, language, notify_direct, notify_potential FROM users WHERE telegram_id = ?")
      .bind(telegramId)
      .first<UserPreferences>();
  }

  async create(telegramId: number, language: Language): Promise<void> {
    const timestamp = new Date().toISOString();
    await this.db
      .prepare(
        "INSERT INTO users (telegram_id, language, created_at, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT (telegram_id) DO NOTHING",
      )
      .bind(telegramId, language, timestamp, timestamp)
      .run();
  }

  async updateLanguage(telegramId: number, language: Language): Promise<void> {
    await this.db
      .prepare("UPDATE users SET language = ?, updated_at = ? WHERE telegram_id = ?")
      .bind(language, new Date().toISOString(), telegramId)
      .run();
  }

  async updateNotification(
    telegramId: number,
    preference: "direct" | "potential",
    enabled: boolean,
  ): Promise<void> {
    const column = preference === "direct" ? "notify_direct" : "notify_potential";
    await this.db
      .prepare(`UPDATE users SET ${column} = ?, updated_at = ? WHERE telegram_id = ?`)
      .bind(enabled ? 1 : 0, new Date().toISOString(), telegramId)
      .run();
  }
}
