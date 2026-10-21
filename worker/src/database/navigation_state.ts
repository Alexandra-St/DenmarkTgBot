import type { ArchiveFilter } from "./publications.ts";

export type NavigationScreen =
  | "main"
  | "news"
  | "archive"
  | "settings"
  | "settings_language"
  | "settings_notifications"
  | "about"
  | "publication_detail";

export type NavigationParent = "main" | "news" | "archive" | "settings";

export interface UserNavigationState {
  screen: NavigationScreen;
  parent_screen: NavigationParent | null;
  publication_kind: "news" | "archive" | null;
  archive_filter: ArchiveFilter | null;
  page: number;
}

export interface NavigationStateRepository {
  get(telegramId: number): Promise<UserNavigationState | null>;
  set(telegramId: number, state: UserNavigationState): Promise<void>;
  getScreenMessageId(telegramId: number): Promise<number | null>;
  replaceScreen(telegramId: number, expectedMessageId: number | null, state: UserNavigationState, nextMessageId: number): Promise<boolean>;
}

export const MAIN_NAVIGATION_STATE: UserNavigationState = {
  screen: "main",
  parent_screen: null,
  publication_kind: null,
  archive_filter: null,
  page: 0,
};

const screens = new Set<NavigationScreen>([
  "main", "news", "archive", "settings", "settings_language",
  "settings_notifications", "about", "publication_detail",
]);
const parents = new Set<NavigationParent>(["main", "news", "archive", "settings"]);
const filters = new Set<ArchiveFilter>(["all", "direct", "potential"]);

export function isUserNavigationState(value: unknown): value is UserNavigationState {
  if (!value || typeof value !== "object") return false;
  const state = value as Record<string, unknown>;
  if (
    typeof state.screen !== "string" || !screens.has(state.screen as NavigationScreen) ||
    (state.parent_screen !== null && (typeof state.parent_screen !== "string" || !parents.has(state.parent_screen as NavigationParent))) ||
    (state.publication_kind !== null && state.publication_kind !== "news" && state.publication_kind !== "archive") ||
    (state.archive_filter !== null && (typeof state.archive_filter !== "string" || !filters.has(state.archive_filter as ArchiveFilter))) ||
    !Number.isSafeInteger(state.page) || (state.page as number) < 0 || (state.page as number) > 9999
  ) return false;

  switch (state.screen) {
    case "main":
      return state.parent_screen === null && state.publication_kind === null && state.archive_filter === null && state.page === 0;
    case "news":
      return state.parent_screen === "main" && state.publication_kind === "news" && state.archive_filter === null;
    case "archive":
      return state.parent_screen === "main" && state.publication_kind === "archive" && filters.has(state.archive_filter as ArchiveFilter);
    case "settings":
    case "about":
      return state.parent_screen === "main" && state.publication_kind === null && state.archive_filter === null && state.page === 0;
    case "settings_language":
    case "settings_notifications":
      return state.parent_screen === "settings" && state.publication_kind === null && state.archive_filter === null && state.page === 0;
    case "publication_detail":
      return (state.parent_screen === "news" && state.publication_kind === "news" && state.archive_filter === null) ||
        (state.parent_screen === "archive" && state.publication_kind === "archive" && filters.has(state.archive_filter as ArchiveFilter));
    default:
      return false;
  }
}

export class D1NavigationStateRepository implements NavigationStateRepository {
  private readonly db: D1Database;

  constructor(db: D1Database) { this.db = db; }

  async get(telegramId: number): Promise<UserNavigationState | null> {
    const row = await this.db.prepare(
      "SELECT screen, parent_screen, publication_kind, archive_filter, page FROM user_navigation_state WHERE telegram_id = ?",
    ).bind(telegramId).first<unknown>();
    if (row === null) return null;
    return isUserNavigationState(row) ? row : null;
  }

  async set(telegramId: number, state: UserNavigationState): Promise<void> {
    if (!isUserNavigationState(state)) throw new TypeError("Invalid navigation state");
    await this.db.prepare(`
      INSERT INTO user_navigation_state
        (telegram_id, screen, parent_screen, publication_kind, archive_filter, page, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT (telegram_id) DO UPDATE SET
        screen = excluded.screen,
        parent_screen = excluded.parent_screen,
        publication_kind = excluded.publication_kind,
        archive_filter = excluded.archive_filter,
        page = excluded.page,
        updated_at = excluded.updated_at
    `).bind(
      telegramId,
      state.screen,
      state.parent_screen,
      state.publication_kind,
      state.archive_filter,
      state.page,
      new Date().toISOString(),
    ).run();
  }

  async getScreenMessageId(telegramId: number): Promise<number | null> {
    const row = await this.db.prepare(
      "SELECT last_screen_message_id FROM user_navigation_state WHERE telegram_id = ?",
    ).bind(telegramId).first<{ last_screen_message_id?: unknown }>();
    const value = row?.last_screen_message_id;
    return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : null;
  }

  async replaceScreen(
    telegramId: number,
    expectedMessageId: number | null,
    state: UserNavigationState,
    nextMessageId: number,
  ): Promise<boolean> {
    if (!isUserNavigationState(state) || !Number.isSafeInteger(nextMessageId) || nextMessageId <= 0) {
      throw new TypeError("Invalid navigation screen");
    }
    const result = await this.db.prepare(`
      INSERT INTO user_navigation_state
        (telegram_id, screen, parent_screen, publication_kind, archive_filter, page, updated_at, last_screen_message_id)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT (telegram_id) DO UPDATE SET
        screen = excluded.screen,
        parent_screen = excluded.parent_screen,
        publication_kind = excluded.publication_kind,
        archive_filter = excluded.archive_filter,
        page = excluded.page,
        updated_at = excluded.updated_at,
        last_screen_message_id = excluded.last_screen_message_id
      WHERE user_navigation_state.last_screen_message_id IS ?
    `).bind(
      telegramId,
      state.screen,
      state.parent_screen,
      state.publication_kind,
      state.archive_filter,
      state.page,
      new Date().toISOString(),
      nextMessageId,
      expectedMessageId,
    ).run();
    return result.meta?.changes === 1;
  }
}
