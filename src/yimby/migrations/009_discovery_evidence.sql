CREATE TABLE IF NOT EXISTS discovery_evidence (
    authority_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id),
    query_key TEXT NOT NULL,
    page INTEGER NOT NULL CHECK(page >= 1),
    digest TEXT NOT NULL REFERENCES evidence(digest),
    PRIMARY KEY (authority_id, run_id, query_key, page, digest)
);

CREATE INDEX IF NOT EXISTS discovery_evidence_digest
    ON discovery_evidence(digest);

CREATE INDEX IF NOT EXISTS discovery_evidence_scope
    ON discovery_evidence(authority_id, query_key, page);
