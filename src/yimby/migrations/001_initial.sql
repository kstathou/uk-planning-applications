CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    authority_id TEXT NOT NULL,
    started_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_queue (
    authority_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    reference TEXT NOT NULL,
    first_run_id TEXT NOT NULL REFERENCES runs(id),
    last_run_id TEXT NOT NULL REFERENCES runs(id),
    PRIMARY KEY (source_id, reference)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    authority_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    authority_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    reference TEXT NOT NULL,
    UNIQUE (source_id, reference)
);

CREATE TABLE IF NOT EXISTS native_versions (
    application_id TEXT NOT NULL REFERENCES applications(id),
    payload_hash TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (application_id, payload_hash)
);

CREATE TABLE IF NOT EXISTS semantic_versions (
    id INTEGER PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    section TEXT NOT NULL,
    semantic_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    normaliser_version TEXT NOT NULL,
    UNIQUE (application_id, section, semantic_hash, normaliser_version)
);

CREATE TABLE IF NOT EXISTS section_current (
    application_id TEXT NOT NULL REFERENCES applications(id),
    section TEXT NOT NULL,
    version_id INTEGER NOT NULL REFERENCES semantic_versions(id),
    PRIMARY KEY (application_id, section)
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    application_id TEXT NOT NULL REFERENCES applications(id),
    observed_at TEXT NOT NULL,
    completeness_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    digest TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    source_url TEXT NOT NULL,
    media_type TEXT NOT NULL
);
