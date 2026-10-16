CREATE TABLE users (
  telegram_id INTEGER PRIMARY KEY,
  language TEXT NOT NULL DEFAULT 'uk' CHECK (language IN ('uk', 'ru')),
  notify_direct INTEGER NOT NULL DEFAULT 1 CHECK (notify_direct IN (0, 1)),
  notify_potential INTEGER NOT NULL DEFAULT 0 CHECK (notify_potential IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE sources (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  base_url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE publications (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
  external_id TEXT NOT NULL,
  external_url TEXT NOT NULL,
  original_title TEXT NOT NULL,
  publication_type TEXT,
  relevance TEXT NOT NULL CHECK (relevance IN ('DIRECT', 'POTENTIAL', 'IRRELEVANT')),
  relevance_reason TEXT,
  current_status TEXT,
  published_at TEXT,
  source_updated_at TEXT,
  discovered_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (source_id, external_id)
);

CREATE TABLE publication_events (
  id INTEGER PRIMARY KEY,
  publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE RESTRICT,
  event_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  previous_status TEXT,
  new_status TEXT,
  occurred_at TEXT,
  detected_at TEXT NOT NULL,
  notify_eligible INTEGER NOT NULL DEFAULT 1 CHECK (notify_eligible IN (0, 1))
);

CREATE TABLE notifications (
  id INTEGER PRIMARY KEY,
  telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE RESTRICT,
  event_id INTEGER NOT NULL REFERENCES publication_events(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK (status IN ('PENDING', 'SENT', 'FAILED')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  created_at TEXT NOT NULL,
  sent_at TEXT,
  last_error TEXT,
  UNIQUE (telegram_id, event_id)
);

CREATE TABLE source_states (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
  stream_key TEXT NOT NULL,
  last_success_at TEXT,
  checkpoint TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE (source_id, stream_key)
);

CREATE INDEX idx_publications_source_id ON publications(source_id);
CREATE INDEX idx_publications_relevance ON publications(relevance);
CREATE INDEX idx_publications_published_at ON publications(published_at DESC);
CREATE INDEX idx_publication_events_publication_id ON publication_events(publication_id);
CREATE INDEX idx_publication_events_detected_at ON publication_events(detected_at DESC);
CREATE INDEX idx_notifications_pending ON notifications(status, created_at) WHERE status = 'PENDING';
CREATE INDEX idx_source_states_source_id ON source_states(source_id);

INSERT INTO sources (code, name, base_url, enabled, created_at, updated_at)
VALUES
  ('folketinget', 'Folketinget Open Data (ODA)', 'https://oda.ft.dk/', 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  ('hoeringsportalen', 'Høringsportalen', 'https://hoeringsportalen.dk/', 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
