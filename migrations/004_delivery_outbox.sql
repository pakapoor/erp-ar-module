-- Durable invoice-delivery events committed atomically with approval.
CREATE TABLE IF NOT EXISTS delivery_outbox (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenant(id),
  entity_id         UUID NOT NULL REFERENCES entity(id),
  invoice_id        UUID NOT NULL REFERENCES invoice(id),
  event_type        VARCHAR(50) NOT NULL,
  payload           JSONB NOT NULL,
  status            VARCHAR(20) NOT NULL DEFAULT 'PENDING',
  attempt_count     INTEGER NOT NULL DEFAULT 0,
  max_attempts      INTEGER NOT NULL DEFAULT 10,
  next_attempt_at   TIMESTAMP NOT NULL DEFAULT NOW(),
  locked_at         TIMESTAMP,
  delivered_at      TIMESTAMP,
  last_error        TEXT,
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMP NOT NULL DEFAULT NOW(),

  CONSTRAINT delivery_outbox_event_unique UNIQUE (invoice_id, event_type),
  CONSTRAINT delivery_outbox_status_valid CHECK (
    status IN ('PENDING', 'PROCESSING', 'DELIVERED', 'DEAD')
  ),
  CONSTRAINT delivery_outbox_attempts_valid CHECK (
    attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts
  )
);

CREATE INDEX IF NOT EXISTS idx_delivery_outbox_claim
  ON delivery_outbox(status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_delivery_outbox_tenant
  ON delivery_outbox(tenant_id);

ALTER TABLE delivery_outbox ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON delivery_outbox
  USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
  )
  WITH CHECK (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
  );

COMMENT ON TABLE delivery_outbox IS
  'Transactional outbox. At-least-once delivery; consumers deduplicate by event id.';
