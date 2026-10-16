ALTER TABLE ingestion_runs ADD COLUMN checkpoint_writes INTEGER NOT NULL DEFAULT 0 CHECK (checkpoint_writes >= 0);

CREATE TRIGGER ingestion_staging_count_processed
AFTER UPDATE OF processed ON ingestion_staging
WHEN OLD.processed = 0 AND NEW.processed = 1
BEGIN
  UPDATE ingestion_runs
  SET processed_count = processed_count + 1,
      had_incomplete = CASE WHEN NEW.source_complete = 0 THEN 1 ELSE had_incomplete END,
      updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
  WHERE run_id = NEW.run_id AND status = 'COMMITTING';
END;

CREATE TRIGGER source_states_checkpoint_cas_update
BEFORE UPDATE OF checkpoint ON source_states
WHEN EXISTS (
  SELECT 1 FROM ingestion_runs r
  WHERE r.source_id = OLD.source_id AND r.status = 'COMMITTING'
    AND json_type(r.proposed_checkpoints_json, '$."' || OLD.stream_key || '"') IS NOT NULL
    AND (
      OLD.checkpoint IS NOT json_extract(r.base_checkpoints_json, '$."' || OLD.stream_key || '"')
      OR NEW.checkpoint IS NOT json_extract(r.proposed_checkpoints_json, '$."' || OLD.stream_key || '"')
    )
)
BEGIN
  SELECT RAISE(ABORT, 'checkpoint_compare_update_failed');
END;

CREATE TRIGGER source_states_checkpoint_cas_insert
BEFORE INSERT ON source_states
WHEN EXISTS (
  SELECT 1 FROM ingestion_runs r
  WHERE r.source_id = NEW.source_id AND r.status = 'COMMITTING'
    AND json_type(r.proposed_checkpoints_json, '$."' || NEW.stream_key || '"') IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM source_states s WHERE s.source_id = NEW.source_id AND s.stream_key = NEW.stream_key)
    AND (
      json_type(r.base_checkpoints_json, '$."' || NEW.stream_key || '"') IS NOT 'null'
      OR NEW.checkpoint IS NOT json_extract(r.proposed_checkpoints_json, '$."' || NEW.stream_key || '"')
    )
)
BEGIN
  SELECT RAISE(ABORT, 'checkpoint_compare_failed');
END;

CREATE TRIGGER source_states_checkpoint_count_update
AFTER UPDATE OF checkpoint ON source_states
WHEN EXISTS (
  SELECT 1 FROM ingestion_runs r
  WHERE r.source_id = NEW.source_id AND r.status = 'COMMITTING'
    AND OLD.checkpoint IS json_extract(r.base_checkpoints_json, '$."' || NEW.stream_key || '"')
    AND NEW.checkpoint IS json_extract(r.proposed_checkpoints_json, '$."' || NEW.stream_key || '"')
)
BEGIN
  UPDATE ingestion_runs SET checkpoint_writes = checkpoint_writes + 1
  WHERE source_id = NEW.source_id AND status = 'COMMITTING';
END;

CREATE TRIGGER source_states_checkpoint_count_insert
AFTER INSERT ON source_states
WHEN EXISTS (
  SELECT 1 FROM ingestion_runs r
  WHERE r.source_id = NEW.source_id AND r.status = 'COMMITTING'
    AND json_type(r.base_checkpoints_json, '$."' || NEW.stream_key || '"') IS 'null'
    AND NEW.checkpoint IS json_extract(r.proposed_checkpoints_json, '$."' || NEW.stream_key || '"')
)
BEGIN
  UPDATE ingestion_runs SET checkpoint_writes = checkpoint_writes + 1
  WHERE source_id = NEW.source_id AND status = 'COMMITTING';
END;

CREATE TRIGGER ingestion_runs_guard_commit
BEFORE UPDATE OF status ON ingestion_runs
WHEN OLD.status = 'COMMITTING' AND NEW.status = 'COMMITTED'
BEGIN
  SELECT CASE WHEN
    (SELECT COUNT(*) FROM ingestion_batches WHERE run_id = NEW.run_id) != NEW.expected_batches
    OR (SELECT COUNT(*) FROM ingestion_staging WHERE run_id = NEW.run_id) != NEW.accepted_count
    OR (SELECT COUNT(*) FROM ingestion_staging WHERE run_id = NEW.run_id AND processed = 1) != NEW.processed_count
    OR EXISTS (SELECT 1 FROM ingestion_staging WHERE run_id = NEW.run_id AND processed = 0)
    OR NEW.had_incomplete != 0
    OR NEW.checkpoint_writes != (SELECT COUNT(*) FROM json_each(NEW.proposed_checkpoints_json))
    OR EXISTS (
      SELECT 1 FROM json_each(NEW.proposed_checkpoints_json) p
      LEFT JOIN source_states s ON s.source_id = NEW.source_id AND s.stream_key = p.key
      WHERE s.id IS NULL OR (p.type = 'null' AND s.checkpoint IS NOT NULL)
        OR (p.type != 'null' AND s.checkpoint IS NOT json(p.value))
    )
  THEN RAISE(ABORT, 'ingestion_finalization_incomplete') END;
END;
