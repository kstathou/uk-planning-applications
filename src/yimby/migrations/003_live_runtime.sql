ALTER TABLE authorities
ADD COLUMN live_readiness TEXT NOT NULL DEFAULT 'blocked';

ALTER TABLE authorities
ADD COLUMN live_reason TEXT NOT NULL DEFAULT 'not assessed';

ALTER TABLE authorities
ADD COLUMN live_evidence_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE authorities
ADD COLUMN live_transport TEXT;

ALTER TABLE run_details
ADD COLUMN browser_time_ms INTEGER NOT NULL DEFAULT 0;
