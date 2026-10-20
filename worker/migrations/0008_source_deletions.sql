ALTER TABLE publications ADD COLUMN source_deleted INTEGER NOT NULL DEFAULT 0
  CHECK (source_deleted IN (0, 1));

CREATE INDEX idx_publications_visible_source_deleted
  ON publications(source_id, source_deleted, source_complete, relevance);
