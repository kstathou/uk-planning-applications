ALTER TABLE native_rebuild_inputs
ADD COLUMN evidence_captures_json TEXT NOT NULL DEFAULT '[]';
