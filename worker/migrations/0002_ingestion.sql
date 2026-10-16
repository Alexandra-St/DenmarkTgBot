ALTER TABLE publications ADD COLUMN source_metadata_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE publications ADD COLUMN normalized_snapshot_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE publications ADD COLUMN relationship_snapshot_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE publications ADD COLUMN ruleset_version TEXT;
ALTER TABLE publications ADD COLUMN source_complete INTEGER NOT NULL DEFAULT 1 CHECK (source_complete IN (0, 1));
ALTER TABLE publications ADD COLUMN historical INTEGER NOT NULL DEFAULT 0 CHECK (historical IN (0, 1));
ALTER TABLE publications ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE publication_events ADD COLUMN change_snapshot_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE ingestion_runs (
  run_id TEXT PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
  mode TEXT NOT NULL CHECK (mode IN ('historical', 'incremental')),
  expected_batches INTEGER NOT NULL CHECK (expected_batches BETWEEN 1 AND 200),
  collection_complete INTEGER NOT NULL CHECK (collection_complete IN (0, 1)),
  status TEXT NOT NULL CHECK (status IN ('OPEN', 'COMMITTING', 'COMMITTED', 'BLOCKED', 'ABORTED')),
  proposed_checkpoints_json TEXT NOT NULL,
  base_checkpoints_json TEXT NOT NULL,
  start_request_hash TEXT NOT NULL,
  had_incomplete INTEGER NOT NULL DEFAULT 0 CHECK (had_incomplete IN (0, 1)),
  accepted_count INTEGER NOT NULL DEFAULT 0 CHECK (accepted_count >= 0),
  processed_count INTEGER NOT NULL DEFAULT 0 CHECK (processed_count >= 0),
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE UNIQUE INDEX idx_ingestion_one_active_run_per_source
  ON ingestion_runs(source_id) WHERE status IN ('OPEN', 'COMMITTING');

CREATE TABLE ingestion_batches (
  run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id) ON DELETE RESTRICT,
  batch_index INTEGER NOT NULL CHECK (batch_index >= 0),
  payload_hash TEXT NOT NULL,
  record_count INTEGER NOT NULL CHECK (record_count BETWEEN 0 AND 50),
  accepted_at TEXT NOT NULL,
  PRIMARY KEY (run_id, batch_index)
);

CREATE TABLE ingestion_staging (
  run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id) ON DELETE RESTRICT,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
  external_id TEXT NOT NULL,
  publication_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  source_complete INTEGER NOT NULL CHECK (source_complete IN (0, 1)),
  processed INTEGER NOT NULL DEFAULT 0 CHECK (processed IN (0, 1)),
  PRIMARY KEY (run_id, source_id, external_id)
);

CREATE INDEX idx_ingestion_staging_pending ON ingestion_staging(run_id, processed, external_id);
