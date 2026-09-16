CREATE TABLE IF NOT EXISTS observation_evidence (
    observation_id INTEGER NOT NULL REFERENCES observations(id),
    digest TEXT NOT NULL REFERENCES evidence(digest),
    PRIMARY KEY (observation_id, digest)
);

CREATE INDEX IF NOT EXISTS observation_evidence_digest
    ON observation_evidence(digest);

INSERT OR IGNORE INTO observation_evidence(observation_id, digest)
SELECT observation.id, linked.value
FROM native_rebuild_inputs AS rebuild
JOIN observations AS observation
    ON observation.id = (
        SELECT MAX(candidate.id)
        FROM observations AS candidate
        WHERE candidate.application_id = rebuild.application_id
    )
CROSS JOIN json_each(rebuild.evidence_digests_json) AS linked
JOIN evidence ON evidence.digest = linked.value;
