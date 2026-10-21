CREATE TABLE user_navigation_state (
  telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
  screen TEXT NOT NULL CHECK (screen IN (
    'main', 'news', 'archive', 'settings', 'settings_language',
    'settings_notifications', 'about', 'publication_detail'
  )),
  parent_screen TEXT CHECK (parent_screen IS NULL OR parent_screen IN (
    'main', 'news', 'archive', 'settings'
  )),
  publication_kind TEXT CHECK (publication_kind IS NULL OR publication_kind IN ('news', 'archive')),
  archive_filter TEXT CHECK (archive_filter IS NULL OR archive_filter IN ('all', 'direct', 'potential')),
  page INTEGER NOT NULL DEFAULT 0 CHECK (page BETWEEN 0 AND 9999),
  updated_at TEXT NOT NULL
);
