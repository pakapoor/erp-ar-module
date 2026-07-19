-- B1 multi-currency foundation: durable provider imports, immutable approved
-- rates, and explicit base-currency snapshots on financial transactions.
BEGIN;

CREATE TABLE IF NOT EXISTS fx_import_job (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider                VARCHAR(30) NOT NULL DEFAULT 'ECB',
  requested_date          DATE NOT NULL,
  provider_effective_date DATE,
  status                  VARCHAR(20) NOT NULL DEFAULT 'PENDING',
  attempt_count           INTEGER NOT NULL DEFAULT 0,
  max_attempts            INTEGER NOT NULL DEFAULT 8,
  next_attempt_at         TIMESTAMP NOT NULL DEFAULT NOW(),
  locked_at               TIMESTAMP,
  completed_at            TIMESTAMP,
  raw_response_hash       CHAR(64),
  last_error              TEXT,
  created_at              TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at              TIMESTAMP NOT NULL DEFAULT NOW(),

  CONSTRAINT fx_import_job_provider_date_unique
    UNIQUE (provider, requested_date),
  CONSTRAINT fx_import_job_status_valid CHECK (
    status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'RETRY', 'DEAD')
  ),
  CONSTRAINT fx_import_job_attempts_valid CHECK (
    attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts
  ),
  CONSTRAINT fx_import_job_hash_valid CHECK (
    raw_response_hash IS NULL OR raw_response_hash ~ '^[0-9a-f]{64}$'
  )
);

CREATE INDEX IF NOT EXISTS idx_fx_import_job_claim
  ON fx_import_job(status, next_attempt_at, created_at);

COMMENT ON TABLE fx_import_job IS
  'Durable system job for fetching one provider FX batch. Not tenant-facing.';

ALTER TABLE exchange_rate
  ADD COLUMN IF NOT EXISTS rate_type VARCHAR(30),
  ADD COLUMN IF NOT EXISTS status VARCHAR(20),
  ADD COLUMN IF NOT EXISTS provider_effective_date DATE,
  ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMP,
  ADD COLUMN IF NOT EXISTS raw_quote_currency CHAR(3),
  ADD COLUMN IF NOT EXISTS raw_quote_rate DECIMAL(20,8),
  ADD COLUMN IF NOT EXISTS is_derived BOOLEAN,
  ADD COLUMN IF NOT EXISTS raw_response_hash CHAR(64),
  ADD COLUMN IF NOT EXISTS import_job_id UUID REFERENCES fx_import_job(id),
  ADD COLUMN IF NOT EXISTS is_manual_override BOOLEAN,
  ADD COLUMN IF NOT EXISTS approved_by UUID REFERENCES app_user(id),
  ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP,
  ADD COLUMN IF NOT EXISTS supersedes_rate_id UUID REFERENCES exchange_rate(id);

UPDATE exchange_rate
SET rate_type = COALESCE(rate_type, 'DAILY_REFERENCE'),
    status = COALESCE(status, 'APPROVED'),
    provider_effective_date = COALESCE(provider_effective_date, effective_date),
    fetched_at = COALESCE(fetched_at, created_at),
    is_derived = COALESCE(is_derived, FALSE),
    is_manual_override = COALESCE(is_manual_override, FALSE),
    approved_at = COALESCE(approved_at, created_at),
    source = COALESCE(source, 'LEGACY');

ALTER TABLE exchange_rate
  ALTER COLUMN source SET NOT NULL,
  ALTER COLUMN rate_type SET NOT NULL,
  ALTER COLUMN status SET NOT NULL,
  ALTER COLUMN provider_effective_date SET NOT NULL,
  ALTER COLUMN fetched_at SET NOT NULL,
  ALTER COLUMN is_derived SET NOT NULL,
  ALTER COLUMN is_manual_override SET NOT NULL;

ALTER TABLE exchange_rate
  ALTER COLUMN rate_type SET DEFAULT 'DAILY_REFERENCE',
  ALTER COLUMN status SET DEFAULT 'PENDING',
  ALTER COLUMN is_derived SET DEFAULT FALSE,
  ALTER COLUMN is_manual_override SET DEFAULT FALSE;

ALTER TABLE exchange_rate
  DROP CONSTRAINT IF EXISTS exchange_rate_unique;

ALTER TABLE exchange_rate
  ADD CONSTRAINT exchange_rate_status_valid CHECK (
    status IN ('PENDING', 'APPROVED', 'REJECTED', 'SUPERSEDED')
  ),
  ADD CONSTRAINT exchange_rate_type_valid CHECK (
    rate_type IN ('DAILY_REFERENCE', 'MANUAL')
  ),
  ADD CONSTRAINT exchange_rate_currency_pair_valid CHECK (
    from_currency ~ '^[A-Z]{3}$'
    AND to_currency ~ '^[A-Z]{3}$'
    AND from_currency <> to_currency
  ),
  ADD CONSTRAINT exchange_rate_raw_quote_positive CHECK (
    raw_quote_rate IS NULL OR raw_quote_rate > 0
  ),
  ADD CONSTRAINT exchange_rate_hash_valid CHECK (
    raw_response_hash IS NULL OR raw_response_hash ~ '^[0-9a-f]{64}$'
  ),
  ADD CONSTRAINT exchange_rate_approval_valid CHECK (
    status <> 'APPROVED' OR approved_at IS NOT NULL
  ),
  ADD CONSTRAINT exchange_rate_manual_approval_valid CHECK (
    NOT is_manual_override OR status <> 'APPROVED' OR approved_by IS NOT NULL
  ),
  ADD CONSTRAINT exchange_rate_supersedes_other CHECK (
    supersedes_rate_id IS NULL OR supersedes_rate_id <> id
  );

DROP INDEX IF EXISTS idx_exchange_rate_lookup;

CREATE INDEX idx_exchange_rate_lookup
  ON exchange_rate(
    tenant_id, from_currency, to_currency, rate_type, effective_date DESC
  );

CREATE UNIQUE INDEX uq_exchange_rate_approved
  ON exchange_rate(
    tenant_id, from_currency, to_currency, effective_date, rate_type
  )
  WHERE status = 'APPROVED';

CREATE UNIQUE INDEX uq_exchange_rate_import_pair
  ON exchange_rate(import_job_id, tenant_id, from_currency, to_currency)
  WHERE import_job_id IS NOT NULL;

ALTER TABLE exchange_rate ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'exchange_rate'
      AND policyname = 'tenant_isolation'
  ) THEN
    CREATE POLICY tenant_isolation ON exchange_rate
      USING (
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
      )
      WITH CHECK (
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
      );
  END IF;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgrelid = 'exchange_rate'::regclass
      AND tgname = 'exchange_rate_audit'
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER exchange_rate_audit
      AFTER INSERT OR UPDATE OR DELETE ON exchange_rate
      FOR EACH ROW EXECUTE FUNCTION write_audit_log();
  END IF;
END;
$$;

ALTER TABLE invoice
  ADD COLUMN IF NOT EXISTS exchange_rate_id UUID REFERENCES exchange_rate(id),
  ADD COLUMN IF NOT EXISTS base_balance_amount DECIMAL(20,4);

UPDATE invoice
SET base_balance_amount = ROUND(balance_amount * exchange_rate, 4)
WHERE base_balance_amount IS NULL;

ALTER TABLE invoice
  ALTER COLUMN base_balance_amount SET NOT NULL,
  ALTER COLUMN base_balance_amount SET DEFAULT 0;

ALTER TABLE invoice
  ADD CONSTRAINT invoice_base_balance_valid CHECK (
    base_balance_amount >= 0 AND base_balance_amount <= base_total_amount
  ),
  ADD CONSTRAINT invoice_fx_rate_reference_valid CHECK (
    (transaction_currency = base_currency AND exchange_rate = 1)
    OR
    (transaction_currency <> base_currency AND exchange_rate_id IS NOT NULL)
  );

CREATE INDEX idx_invoice_exchange_rate
  ON invoice(exchange_rate_id)
  WHERE exchange_rate_id IS NOT NULL;

ALTER TABLE payment
  ADD COLUMN IF NOT EXISTS exchange_rate_id UUID REFERENCES exchange_rate(id),
  ADD COLUMN IF NOT EXISTS base_allocated_amount DECIMAL(20,4),
  ADD COLUMN IF NOT EXISTS base_unallocated_amount DECIMAL(20,4);

UPDATE payment
SET base_allocated_amount = ROUND(allocated_amount * exchange_rate, 4),
    base_unallocated_amount = base_amount - ROUND(allocated_amount * exchange_rate, 4)
WHERE base_allocated_amount IS NULL OR base_unallocated_amount IS NULL;

ALTER TABLE payment
  ALTER COLUMN base_allocated_amount SET NOT NULL,
  ALTER COLUMN base_allocated_amount SET DEFAULT 0,
  ALTER COLUMN base_unallocated_amount SET NOT NULL,
  ALTER COLUMN base_unallocated_amount SET DEFAULT 0;

ALTER TABLE payment
  ADD CONSTRAINT payment_base_allocation_valid CHECK (
    base_allocated_amount >= 0
    AND base_unallocated_amount >= 0
    AND base_amount = base_allocated_amount + base_unallocated_amount
  ),
  ADD CONSTRAINT payment_fx_rate_reference_valid CHECK (
    (transaction_currency = base_currency AND exchange_rate = 1)
    OR
    (transaction_currency <> base_currency AND exchange_rate_id IS NOT NULL)
  );

CREATE INDEX idx_payment_exchange_rate
  ON payment(exchange_rate_id)
  WHERE exchange_rate_id IS NOT NULL;

ALTER TABLE payment_allocation
  ADD COLUMN IF NOT EXISTS base_payment_amount DECIMAL(20,4),
  ADD COLUMN IF NOT EXISTS base_ar_amount DECIMAL(20,4);

UPDATE payment_allocation allocation
SET base_payment_amount = ROUND(allocation.amount_allocated * payment.exchange_rate, 4),
    base_ar_amount = ROUND(allocation.amount_allocated * invoice.exchange_rate, 4),
    fx_gain_loss =
      ROUND(allocation.amount_allocated * payment.exchange_rate, 4)
      - ROUND(allocation.amount_allocated * invoice.exchange_rate, 4)
FROM payment, invoice
WHERE payment.id = allocation.payment_id
  AND invoice.id = allocation.invoice_id
  AND (
    allocation.base_payment_amount IS NULL
    OR allocation.base_ar_amount IS NULL
  );

ALTER TABLE payment_allocation
  ALTER COLUMN base_payment_amount SET NOT NULL,
  ALTER COLUMN base_payment_amount SET DEFAULT 0,
  ALTER COLUMN base_ar_amount SET NOT NULL,
  ALTER COLUMN base_ar_amount SET DEFAULT 0;

ALTER TABLE payment_allocation
  ADD CONSTRAINT allocation_base_amounts_valid CHECK (
    base_payment_amount > 0 AND base_ar_amount > 0
  ),
  ADD CONSTRAINT allocation_realized_fx_valid CHECK (
    fx_gain_loss = base_payment_amount - base_ar_amount
  );

COMMIT;

