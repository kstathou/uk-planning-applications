CREATE TABLE IF NOT EXISTS discovery_evidence (
    authority_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id),
    digest TEXT NOT NULL REFERENCES evidence(digest),
    PRIMARY KEY (authority_id, run_id, digest)
);

CREATE INDEX IF NOT EXISTS discovery_evidence_authority_digest
    ON discovery_evidence(authority_id, digest);
