ALTER TABLE discovery_queue
ADD COLUMN locator TEXT;

ALTER TABLE applications
ADD COLUMN locator TEXT;

ALTER TABLE retry_queue
ADD COLUMN locator TEXT;

ALTER TABLE native_rebuild_inputs
ADD COLUMN locator TEXT;
