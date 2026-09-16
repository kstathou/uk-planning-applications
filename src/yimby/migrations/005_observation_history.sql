CREATE TABLE IF NOT EXISTS observation_native_versions (
    observation_id INTEGER PRIMARY KEY REFERENCES observations(id),
    application_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    FOREIGN KEY (application_id, payload_hash)
        REFERENCES native_versions(application_id, payload_hash)
);

CREATE TABLE IF NOT EXISTS observation_section_versions (
    observation_id INTEGER NOT NULL REFERENCES observations(id),
    section TEXT NOT NULL,
    version_id INTEGER NOT NULL REFERENCES semantic_versions(id),
    PRIMARY KEY (observation_id, section)
);

CREATE INDEX IF NOT EXISTS observation_section_history
    ON observation_section_versions(section, observation_id);
