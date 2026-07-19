-- Extend the transactional outbox with its broker-publication lifecycle.
ALTER TABLE delivery_outbox
  ADD COLUMN IF NOT EXISTS publish_attempt_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS max_publish_attempts INTEGER NOT NULL DEFAULT 10,
  ADD COLUMN IF NOT EXISTS sqs_message_id VARCHAR(100),
  ADD COLUMN IF NOT EXISTS published_at TIMESTAMP;

ALTER TABLE delivery_outbox
  ALTER COLUMN max_attempts SET DEFAULT 3;

-- Existing rows may already have more than three direct-delivery attempts.
-- Preserve their valid counters; new events use the SQS redrive limit of three.
UPDATE delivery_outbox
SET max_attempts = GREATEST(attempt_count, 3)
WHERE max_attempts < GREATEST(attempt_count, 3);

ALTER TABLE delivery_outbox
  DROP CONSTRAINT IF EXISTS delivery_outbox_status_valid;

ALTER TABLE delivery_outbox
  ADD CONSTRAINT delivery_outbox_status_valid CHECK (
    status IN (
      'PENDING', 'PROCESSING', 'PUBLISHED', 'DELIVERING',
      'DELIVERED', 'DEAD'
    )
  );

ALTER TABLE delivery_outbox
  DROP CONSTRAINT IF EXISTS delivery_outbox_publish_attempts_valid;

ALTER TABLE delivery_outbox
  ADD CONSTRAINT delivery_outbox_publish_attempts_valid CHECK (
    publish_attempt_count >= 0
    AND max_publish_attempts > 0
    AND publish_attempt_count <= max_publish_attempts
  );

CREATE INDEX IF NOT EXISTS idx_delivery_outbox_invoice
  ON delivery_outbox(tenant_id, entity_id, invoice_id, created_at);

COMMENT ON COLUMN delivery_outbox.sqs_message_id IS
  'Latest SQS message ID; duplicate publishes remain safe via the outbox event ID.';
