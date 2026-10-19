ALTER TABLE ingestion_runs ADD COLUMN state_kind TEXT NOT NULL DEFAULT 'checkpoint'
  CHECK (state_kind IN ('checkpoint', 'snapshot'));
ALTER TABLE ingestion_runs ADD COLUMN base_snapshot_json TEXT;
ALTER TABLE ingestion_runs ADD COLUMN target_snapshot_json TEXT;
ALTER TABLE ingestion_runs ADD COLUMN snapshot_state_writes INTEGER NOT NULL DEFAULT 0
  CHECK (snapshot_state_writes >= 0);

CREATE TABLE source_snapshot_states (
  source_id INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE RESTRICT,
  backup_sha256 TEXT NOT NULL CHECK (
    length(backup_sha256) = 64 AND backup_sha256 = lower(backup_sha256) AND
    length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(backup_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0
  ),
  backup_finish_date TEXT NOT NULL CHECK (
    length(backup_finish_date) = 23 AND
    substr(backup_finish_date, 5, 1) = '-' AND substr(backup_finish_date, 8, 1) = '-' AND
    substr(backup_finish_date, 11, 1) = ' ' AND substr(backup_finish_date, 14, 1) = ':' AND
    substr(backup_finish_date, 17, 1) = ':' AND substr(backup_finish_date, 20, 1) = '.' AND
    substr(backup_finish_date, 1, 4) = printf('%04d', CAST(substr(backup_finish_date, 1, 4) AS INTEGER)) AND
    substr(backup_finish_date, 6, 2) = printf('%02d', CAST(substr(backup_finish_date, 6, 2) AS INTEGER)) AND
    substr(backup_finish_date, 9, 2) = printf('%02d', CAST(substr(backup_finish_date, 9, 2) AS INTEGER)) AND
    substr(backup_finish_date, 12, 2) = printf('%02d', CAST(substr(backup_finish_date, 12, 2) AS INTEGER)) AND
    substr(backup_finish_date, 15, 2) = printf('%02d', CAST(substr(backup_finish_date, 15, 2) AS INTEGER)) AND
    substr(backup_finish_date, 18, 2) = printf('%02d', CAST(substr(backup_finish_date, 18, 2) AS INTEGER)) AND
    substr(backup_finish_date, 21, 3) = printf('%03d', CAST(substr(backup_finish_date, 21, 3) AS INTEGER)) AND
    CAST(substr(backup_finish_date, 6, 2) AS INTEGER) BETWEEN 1 AND 12 AND
    CAST(substr(backup_finish_date, 9, 2) AS INTEGER) BETWEEN 1 AND 31 AND
    CAST(substr(backup_finish_date, 12, 2) AS INTEGER) BETWEEN 0 AND 23 AND
    CAST(substr(backup_finish_date, 15, 2) AS INTEGER) BETWEEN 0 AND 59 AND
    CAST(substr(backup_finish_date, 18, 2) AS INTEGER) BETWEEN 0 AND 59
  ),
  schema_fingerprint TEXT NOT NULL CHECK (
    length(schema_fingerprint) = 64 AND schema_fingerprint = lower(schema_fingerprint) AND
    length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(schema_fingerprint, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0
  ),
  generation INTEGER NOT NULL CHECK (generation >= 1),
  last_run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id) ON DELETE RESTRICT,
  initialized_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Snapshot runs cannot alter OData cursors while they are being committed.
CREATE TRIGGER source_states_reject_snapshot_run_insert
BEFORE INSERT ON source_states
WHEN EXISTS (
  SELECT 1 FROM ingestion_runs r
  WHERE r.source_id = NEW.source_id AND r.status = 'COMMITTING' AND r.state_kind = 'snapshot'
)
BEGIN
  SELECT RAISE(ABORT, 'snapshot_run_cannot_write_checkpoints');
END;

CREATE TRIGGER source_states_reject_snapshot_run_update
BEFORE UPDATE ON source_states
WHEN EXISTS (
  SELECT 1 FROM ingestion_runs r
  WHERE r.source_id = OLD.source_id AND r.status = 'COMMITTING' AND r.state_kind = 'snapshot'
)
BEGIN
  SELECT RAISE(ABORT, 'snapshot_run_cannot_write_checkpoints');
END;

CREATE TRIGGER source_states_reject_snapshot_run_delete
BEFORE DELETE ON source_states
WHEN EXISTS (
  SELECT 1 FROM ingestion_runs r
  WHERE r.source_id = OLD.source_id AND r.status = 'COMMITTING' AND r.state_kind = 'snapshot'
)
BEGIN
  SELECT RAISE(ABORT, 'snapshot_run_cannot_write_checkpoints');
END;

-- Initial state is allowed only for a committing snapshot run whose target matches exactly.
CREATE TRIGGER source_snapshot_states_guard_insert
BEFORE INSERT ON source_snapshot_states
WHEN NOT EXISTS (
  SELECT 1 FROM ingestion_runs r
  WHERE r.run_id = NEW.last_run_id
    AND r.source_id = NEW.source_id
    AND r.status = 'COMMITTING'
    AND r.state_kind = 'snapshot'
    AND r.mode = 'historical'
    AND r.base_snapshot_json IS NULL
    AND r.checkpoint_writes = 0
    AND json_valid(r.proposed_checkpoints_json)
    AND json_type(r.proposed_checkpoints_json) = 'object'
    AND NOT EXISTS (SELECT 1 FROM json_each(r.proposed_checkpoints_json))
    AND json_valid(r.base_checkpoints_json)
    AND json_type(r.base_checkpoints_json) = 'object'
    AND NOT EXISTS (SELECT 1 FROM json_each(r.base_checkpoints_json))
    AND json_valid(r.target_snapshot_json)
    AND json_type(r.target_snapshot_json) = 'object'
    AND (SELECT COUNT(*) FROM json_each(r.target_snapshot_json)) = 3
    AND json_type(r.target_snapshot_json, '$.backup_sha256') = 'text'
    AND json_type(r.target_snapshot_json, '$.backup_finish_date') = 'text'
    AND json_type(r.target_snapshot_json, '$.schema_fingerprint') = 'text'
    AND json_extract(r.target_snapshot_json, '$.backup_sha256') = NEW.backup_sha256
    AND json_extract(r.target_snapshot_json, '$.backup_finish_date') = NEW.backup_finish_date
    AND json_extract(r.target_snapshot_json, '$.schema_fingerprint') = NEW.schema_fingerprint
    AND NEW.generation = 1
)
BEGIN
  SELECT RAISE(ABORT, 'snapshot_compare_insert_failed');
END;

CREATE TRIGGER source_snapshot_states_count_insert
AFTER INSERT ON source_snapshot_states
BEGIN
  UPDATE ingestion_runs
  SET snapshot_state_writes = snapshot_state_writes + 1
  WHERE run_id = NEW.last_run_id AND source_id = NEW.source_id
    AND status = 'COMMITTING' AND state_kind = 'snapshot';
END;

-- Progression is a compare-and-swap from the persisted base to one exact target.
CREATE TRIGGER source_snapshot_states_guard_update
BEFORE UPDATE ON source_snapshot_states
WHEN NOT EXISTS (
  SELECT 1 FROM ingestion_runs r
  WHERE r.run_id = NEW.last_run_id
    AND r.source_id = OLD.source_id
    AND NEW.source_id = OLD.source_id
    AND r.status = 'COMMITTING'
    AND r.state_kind = 'snapshot'
    AND r.mode = 'incremental'
    AND r.base_snapshot_json IS NOT NULL
    AND r.checkpoint_writes = 0
    AND json_valid(r.proposed_checkpoints_json)
    AND json_type(r.proposed_checkpoints_json) = 'object'
    AND NOT EXISTS (SELECT 1 FROM json_each(r.proposed_checkpoints_json))
    AND json_valid(r.base_checkpoints_json)
    AND json_type(r.base_checkpoints_json) = 'object'
    AND NOT EXISTS (SELECT 1 FROM json_each(r.base_checkpoints_json))
    AND json_valid(r.base_snapshot_json)
    AND json_type(r.base_snapshot_json) = 'object'
    AND (SELECT COUNT(*) FROM json_each(r.base_snapshot_json)) = 3
    AND json_type(r.base_snapshot_json, '$.backup_sha256') = 'text'
    AND json_type(r.base_snapshot_json, '$.backup_finish_date') = 'text'
    AND json_type(r.base_snapshot_json, '$.schema_fingerprint') = 'text'
    AND json_extract(r.base_snapshot_json, '$.backup_sha256') = OLD.backup_sha256
    AND json_extract(r.base_snapshot_json, '$.backup_finish_date') = OLD.backup_finish_date
    AND json_extract(r.base_snapshot_json, '$.schema_fingerprint') = OLD.schema_fingerprint
    AND json_valid(r.target_snapshot_json)
    AND json_type(r.target_snapshot_json) = 'object'
    AND (SELECT COUNT(*) FROM json_each(r.target_snapshot_json)) = 3
    AND json_type(r.target_snapshot_json, '$.backup_sha256') = 'text'
    AND json_type(r.target_snapshot_json, '$.backup_finish_date') = 'text'
    AND json_type(r.target_snapshot_json, '$.schema_fingerprint') = 'text'
    AND json_extract(r.target_snapshot_json, '$.backup_sha256') = NEW.backup_sha256
    AND json_extract(r.target_snapshot_json, '$.backup_finish_date') = NEW.backup_finish_date
    AND json_extract(r.target_snapshot_json, '$.schema_fingerprint') = NEW.schema_fingerprint
    AND NEW.generation = OLD.generation + 1
)
BEGIN
  SELECT RAISE(ABORT, 'snapshot_compare_update_failed');
END;

CREATE TRIGGER source_snapshot_states_count_update
AFTER UPDATE ON source_snapshot_states
BEGIN
  UPDATE ingestion_runs
  SET snapshot_state_writes = snapshot_state_writes + 1
  WHERE run_id = NEW.last_run_id AND source_id = NEW.source_id
    AND status = 'COMMITTING' AND state_kind = 'snapshot';
END;

-- There is no supported operation that deletes a committed snapshot identity.
CREATE TRIGGER source_snapshot_states_reject_delete
BEFORE DELETE ON source_snapshot_states
BEGIN
  SELECT RAISE(ABORT, 'snapshot_state_delete_forbidden');
END;

CREATE TRIGGER ingestion_runs_guard_state_kind_commit
BEFORE UPDATE OF status ON ingestion_runs
WHEN OLD.status = 'COMMITTING' AND NEW.status = 'COMMITTED'
BEGIN
  SELECT CASE WHEN NEW.state_kind = 'checkpoint' AND (
    NEW.base_snapshot_json IS NOT NULL OR
    NEW.target_snapshot_json IS NOT NULL OR
    NEW.snapshot_state_writes != 0
  ) THEN RAISE(ABORT, 'checkpoint_snapshot_state_mismatch') END;

  SELECT CASE WHEN NEW.state_kind = 'snapshot' AND (
    NEW.checkpoint_writes != 0 OR
    NEW.snapshot_state_writes != 1 OR
    NOT json_valid(NEW.proposed_checkpoints_json) OR
    json_type(NEW.proposed_checkpoints_json) != 'object' OR
    EXISTS (SELECT 1 FROM json_each(NEW.proposed_checkpoints_json)) OR
    NOT json_valid(NEW.base_checkpoints_json) OR
    json_type(NEW.base_checkpoints_json) != 'object' OR
    EXISTS (SELECT 1 FROM json_each(NEW.base_checkpoints_json)) OR
    NEW.target_snapshot_json IS NULL OR
    (NEW.mode = 'historical' AND NEW.base_snapshot_json IS NOT NULL) OR
    (NEW.mode = 'incremental' AND NEW.base_snapshot_json IS NULL) OR
    NOT json_valid(NEW.target_snapshot_json) OR
    json_type(NEW.target_snapshot_json) != 'object' OR
    (SELECT COUNT(*) FROM json_each(NEW.target_snapshot_json)) != 3 OR
    NOT EXISTS (
      SELECT 1 FROM source_snapshot_states s
      WHERE s.source_id = NEW.source_id
        AND s.backup_sha256 = json_extract(NEW.target_snapshot_json, '$.backup_sha256')
        AND s.backup_finish_date = json_extract(NEW.target_snapshot_json, '$.backup_finish_date')
        AND s.schema_fingerprint = json_extract(NEW.target_snapshot_json, '$.schema_fingerprint')
        AND s.last_run_id = NEW.run_id
    )
  ) THEN RAISE(ABORT, 'snapshot_ingestion_state_incomplete') END;
END;
