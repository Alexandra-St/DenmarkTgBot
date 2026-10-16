-- Add an atomic delivery claim so concurrent dispatchers cannot send one queue row twice.
CREATE TABLE notifications_new (
  id INTEGER PRIMARY KEY,
  telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE RESTRICT,
  event_id INTEGER NOT NULL REFERENCES publication_events(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'FAILED')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  created_at TEXT NOT NULL,
  sent_at TEXT,
  last_error TEXT,
  UNIQUE (telegram_id, event_id)
);

INSERT INTO notifications_new
  (id, telegram_id, event_id, status, attempts, created_at, sent_at, last_error)
SELECT id, telegram_id, event_id, status, attempts, created_at, sent_at, last_error
FROM notifications;

DROP INDEX IF EXISTS idx_notifications_pending;
DROP TABLE notifications;
ALTER TABLE notifications_new RENAME TO notifications;

CREATE INDEX idx_notifications_pending ON notifications(status, created_at) WHERE status = 'PENDING';
