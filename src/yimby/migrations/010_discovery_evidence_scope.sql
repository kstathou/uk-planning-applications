ALTER TABLE discovery_evidence RENAME TO discovery_evidence_v8;

CREATE TABLE discovery_evidence (
    authority_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id),
    query_key TEXT NOT NULL,
    page INTEGER NOT NULL CHECK(page >= 1),
    digest TEXT NOT NULL REFERENCES evidence(digest),
    PRIMARY KEY (authority_id, run_id, query_key, page, digest)
);

INSERT INTO discovery_evidence(
    authority_id, run_id, query_key, page, digest
)
SELECT authority_id, run_id, 'legacy-unscoped', 1, digest
FROM discovery_evidence_v8;

DROP TABLE discovery_evidence_v8;

CREATE INDEX discovery_evidence_digest
    ON discovery_evidence(digest);

CREATE INDEX discovery_evidence_scope
    ON discovery_evidence(authority_id, query_key, page);
