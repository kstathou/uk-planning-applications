CREATE TABLE IF NOT EXISTS qualification_lineage (
    authority_id TEXT NOT NULL,
    qualification TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase = 'qualified'),
    scope_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (authority_id, qualification)
);
