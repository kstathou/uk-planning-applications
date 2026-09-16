ALTER TABLE observation_evidence
ADD COLUMN response_url TEXT;

ALTER TABLE discovery_evidence
ADD COLUMN response_url TEXT;

ALTER TABLE discovery_evidence
ADD COLUMN request_url TEXT;

ALTER TABLE discovery_evidence
ADD COLUMN request_method TEXT;

ALTER TABLE discovery_evidence
ADD COLUMN request_form_json TEXT;
