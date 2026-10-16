CREATE TABLE source_ingestion_baselines (
  source_id INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE RESTRICT,
  baseline_run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id) ON DELETE RESTRICT,
  initialized_at TEXT NOT NULL
);

CREATE TRIGGER ingestion_runs_guard_baseline
BEFORE UPDATE OF status ON ingestion_runs
WHEN OLD.status = 'COMMITTING' AND NEW.status = 'COMMITTED'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM source_ingestion_baselines b WHERE b.source_id = NEW.source_id
  ) THEN RAISE(ABORT, 'historical_baseline_required') END;
END;
