-- One diagnostic canary attempt per database. A claimed row is never reset automatically.
CREATE TABLE notification_canary_attempts (
  singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
  telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
  request_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('SENDING', 'SENT', 'FAILED')),
  created_at TEXT NOT NULL,
  completed_at TEXT,
  last_error TEXT
);
