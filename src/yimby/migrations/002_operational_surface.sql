CREATE TABLE IF NOT EXISTS authorities (
    authority_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    implementation_status TEXT NOT NULL,
    transport_mode TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    source_manifest_json TEXT NOT NULL,
    last_success_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_details (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    status TEXT NOT NULL,
    finished_at TEXT,
    request_count INTEGER NOT NULL DEFAULT 0,
    transferred_bytes INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    storage_growth_bytes INTEGER NOT NULL DEFAULT 0,
    failure_message TEXT
);

CREATE TABLE IF NOT EXISTS retry_queue (
    id INTEGER PRIMARY KEY,
    authority_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    reference TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE (authority_id, source_id, reference)
);

CREATE TABLE IF NOT EXISTS refresh_schedules (
    application_id TEXT PRIMARY KEY REFERENCES applications(id),
    next_refresh_at TEXT NOT NULL,
    cadence TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failure_diagnostics (
    id INTEGER PRIMARY KEY,
    authority_id TEXT NOT NULL,
    run_id TEXT REFERENCES runs(id),
    occurred_at TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    retryable INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS application_details (
    application_id TEXT PRIMARY KEY REFERENCES applications(id),
    application_type TEXT,
    decision TEXT,
    address TEXT,
    received_date TEXT,
    validated_date TEXT,
    decision_date TEXT,
    comment_count INTEGER NOT NULL,
    source_url TEXT,
    parties_json TEXT NOT NULL,
    officer_name TEXT,
    constraints_json TEXT NOT NULL,
    conditions_json TEXT NOT NULL,
    consultations_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS application_locations (
    application_id TEXT PRIMARY KEY REFERENCES applications(id),
    bng_easting REAL NOT NULL,
    bng_northing REAL NOT NULL,
    longitude REAL NOT NULL,
    latitude REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS application_aliases (
    application_id TEXT NOT NULL REFERENCES applications(id),
    alias TEXT NOT NULL,
    source_id TEXT NOT NULL,
    PRIMARY KEY (application_id, alias)
);

CREATE TABLE IF NOT EXISTS application_dates (
    application_id TEXT NOT NULL REFERENCES applications(id),
    date_kind TEXT NOT NULL,
    date_value TEXT NOT NULL,
    PRIMARY KEY (application_id, date_kind)
);

CREATE TABLE IF NOT EXISTS document_metadata (
    application_id TEXT NOT NULL REFERENCES applications(id),
    document_key TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    PRIMARY KEY (application_id, document_key)
);

CREATE TABLE IF NOT EXISTS comment_records (
    application_id TEXT NOT NULL REFERENCES applications(id),
    comment_id TEXT NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (application_id, comment_id)
);

CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    event_type TEXT NOT NULL,
    event_at TEXT NOT NULL,
    details TEXT,
    semantic_hash TEXT NOT NULL,
    UNIQUE (application_id, semantic_hash)
);

CREATE TABLE IF NOT EXISTS application_relationships (
    application_id TEXT NOT NULL REFERENCES applications(id),
    related_reference TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    PRIMARY KEY (application_id, related_reference, relationship_type)
);

CREATE TABLE IF NOT EXISTS suppression_corrections (
    application_id TEXT PRIMARY KEY REFERENCES applications(id),
    suppressed INTEGER NOT NULL,
    reason TEXT NOT NULL,
    corrected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS native_rebuild_inputs (
    application_id TEXT PRIMARY KEY REFERENCES applications(id),
    authority_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    reference TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    completeness_json TEXT NOT NULL,
    evidence_digests_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS normalisation_rebuilds (
    id INTEGER PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    normaliser_version TEXT NOT NULL,
    rebuilt_at TEXT NOT NULL
);
