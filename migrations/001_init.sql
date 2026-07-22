-- ============================================================
-- ERP AR Module — Consolidated Schema
-- migrations/init.sql
-- ============================================================
-- Single file representing the complete current schema state.
-- Supersedes the incremental 001-010 migration files; this is
-- the final state, not a sequence of deltas.
--
-- Assumptions:
-- - PostgreSQL 14+ (pg_cron extras assume 16, see 003_setup_pg_cron.sh)
-- - All monetary amounts stored as DECIMAL(20,4)
-- - All timestamps in UTC
-- - Row Level Security enforced at DB level
-- - GAAP accrual accounting standard
-- - Status/type "enums" are VARCHAR + CHECK constraints — this schema
--   never uses native Postgres ENUM types
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- HELPER FUNCTIONS
-- ============================================================

-- Writes to audit_log on any table change. Attached per-table below.
CREATE OR REPLACE FUNCTION write_audit_log()
RETURNS TRIGGER AS $$
BEGIN
  INSERT INTO audit_log (
    id, tenant_id, table_name, record_id,
    action, old_value, new_value,
    changed_by, changed_at
  ) VALUES (
    gen_random_uuid(),
    COALESCE(NEW.tenant_id, OLD.tenant_id),
    TG_TABLE_NAME,
    COALESCE(NEW.id, OLD.id),
    TG_OP,
    CASE WHEN TG_OP = 'DELETE' OR TG_OP = 'UPDATE'
         THEN row_to_json(OLD) ELSE NULL END,
    CASE WHEN TG_OP = 'INSERT' OR TG_OP = 'UPDATE'
         THEN row_to_json(NEW) ELSE NULL END,
    current_setting('app.current_user_id', true)::uuid,
    NOW()
  );
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Prevents posting to closed/locked periods. Invoice creation stays DRAFT;
-- the period is only validated (and stamped onto the row) on DRAFT -> APPROVED.
CREATE OR REPLACE FUNCTION check_period_open()
RETURNS TRIGGER AS $$
DECLARE
  period_record RECORD;
BEGIN
  -- Invoice creation remains DRAFT. Validate the document-date period only
  -- when the invoice transitions to APPROVED and creates its GL entry.
  IF NOT (OLD.status = 'DRAFT' AND NEW.status = 'APPROVED') THEN
    RETURN NEW;
  END IF;

  SELECT id, status INTO period_record
  FROM accounting_period
  WHERE tenant_id = NEW.tenant_id
    AND entity_id = NEW.entity_id
    AND start_date <= NEW.invoice_date
    AND end_date >= NEW.invoice_date;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'No accounting period exists for invoice date %', NEW.invoice_date;
  END IF;

  IF period_record.status <> 'OPEN' THEN
    RAISE EXCEPTION 'Cannot post to % period. Status: %',
      NEW.invoice_date, period_record.status;
  END IF;

  NEW.period_id := period_record.id;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION check_journal_period_open()
RETURNS TRIGGER AS $$
DECLARE
  period_status VARCHAR;
BEGIN
  SELECT status INTO period_status
  FROM accounting_period
  WHERE id = NEW.period_id
    AND tenant_id = NEW.tenant_id
    AND entity_id = NEW.entity_id
    AND start_date <= NEW.entry_date
    AND end_date >= NEW.entry_date;

  IF NOT FOUND OR period_status <> 'OPEN' THEN
    RAISE EXCEPTION 'Journal entry date % must belong to an OPEN period', NEW.entry_date;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION check_period_transition()
RETURNS TRIGGER AS $$
BEGIN
  IF OLD.status = 'LOCKED' AND NEW.status IS DISTINCT FROM OLD.status THEN
    RAISE EXCEPTION 'LOCKED accounting periods cannot be reopened or changed';
  END IF;

  IF OLD.status = 'OPEN' AND NEW.status = 'LOCKED' THEN
    RAISE EXCEPTION 'An OPEN period must be CLOSED before it can be LOCKED';
  END IF;

  IF OLD.status = 'CLOSED' AND NEW.status = 'OPEN'
     AND (NEW.reopened_by IS NULL OR NEW.reopened_at IS NULL
          OR NULLIF(BTRIM(NEW.reopen_reason), '') IS NULL) THEN
    RAISE EXCEPTION 'Reopening a CLOSED period requires actor, timestamp, and reason';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- TABLE: TENANT
-- Top level — one per ERP customer
-- ============================================================
CREATE TABLE tenant (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name              VARCHAR(255) NOT NULL,
  base_currency     CHAR(3) NOT NULL DEFAULT 'USD',
  is_active         BOOLEAN NOT NULL DEFAULT TRUE,
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMP NOT NULL DEFAULT NOW(),

  CONSTRAINT tenant_name_unique UNIQUE (name),
  CONSTRAINT tenant_currency_valid CHECK (char_length(base_currency) = 3)
);

COMMENT ON TABLE tenant IS 'Top level entity — one per ERP customer';

-- ============================================================
-- TABLE: ENTITY
-- Legal subsidiary within a tenant. Self-referential for hierarchy.
-- ============================================================
CREATE TABLE entity (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenant(id),
  parent_entity_id  UUID REFERENCES entity(id),  -- NULL = root entity
  name              VARCHAR(255) NOT NULL,
  currency          CHAR(3) NOT NULL,
  is_active         BOOLEAN NOT NULL DEFAULT TRUE,
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),

  CONSTRAINT entity_currency_valid CHECK (char_length(currency) = 3),
  CONSTRAINT entity_name_tenant_unique UNIQUE (tenant_id, name)
);

CREATE INDEX idx_entity_tenant ON entity(tenant_id);
CREATE INDEX idx_entity_parent ON entity(parent_entity_id);

ALTER TABLE entity ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON entity
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- ============================================================
-- TABLE: GL ACCOUNT
-- Chart of accounts per entity
-- ============================================================
CREATE TABLE gl_account (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenant(id),
  entity_id         UUID NOT NULL REFERENCES entity(id),
  account_code      VARCHAR(20) NOT NULL,
  account_name      VARCHAR(255) NOT NULL,
  account_type      VARCHAR(50) NOT NULL,
  is_active         BOOLEAN NOT NULL DEFAULT TRUE,
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),

  CONSTRAINT gl_account_type_valid CHECK (
    account_type IN ('ASSET', 'LIABILITY', 'REVENUE', 'EXPENSE', 'EQUITY')
  ),
  CONSTRAINT gl_account_code_entity_unique UNIQUE (entity_id, account_code)
);

CREATE INDEX idx_gl_account_tenant ON gl_account(tenant_id);
CREATE INDEX idx_gl_account_entity ON gl_account(entity_id);
CREATE INDEX idx_gl_account_code ON gl_account(entity_id, account_code);

ALTER TABLE gl_account ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON gl_account
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- ============================================================
-- TABLE: USER
-- System users with roles. Self-referential for manager hierarchy.
-- ============================================================
CREATE TABLE app_user (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenant(id),
  entity_id         UUID NOT NULL REFERENCES entity(id),
  manager_id        UUID REFERENCES app_user(id),
  delegate_id       UUID REFERENCES app_user(id),
  delegate_expiry   TIMESTAMP,
  name              VARCHAR(255) NOT NULL,
  email             VARCHAR(255) NOT NULL,
  roles             TEXT[] NOT NULL DEFAULT '{}',
  is_active         BOOLEAN NOT NULL DEFAULT TRUE,
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),

  CONSTRAINT user_email_tenant_unique UNIQUE (tenant_id, email),
  CONSTRAINT user_roles_valid CHECK (
    roles <@ ARRAY[
      'invoice_creator', 'invoice_approver',
      'payment_recorder', 'cfo',
      'auditor', 'system_admin'
    ]::text[]
  )
);

CREATE INDEX idx_user_tenant ON app_user(tenant_id);
CREATE INDEX idx_user_entity ON app_user(entity_id);
CREATE INDEX idx_user_email ON app_user(tenant_id, email);

ALTER TABLE app_user ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app_user
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

COMMENT ON COLUMN app_user.roles IS
  'Array of roles. SOX requires invoice creator != approver (enforced in invoice.check constraint).';

-- ============================================================
-- TABLE: CUSTOMER
-- External companies being invoiced. PII fields marked for KMS
-- encryption at app layer.
-- ============================================================
CREATE TABLE customer (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenant(id),
  entity_id         UUID NOT NULL REFERENCES entity(id),
  name              VARCHAR(255) NOT NULL,
  email             VARCHAR(255),
  phone             VARCHAR(50),
  billing_address   TEXT,
  shipping_address  TEXT,
  tax_identifier    TEXT,        -- KMS encrypted at app layer (GST/VAT/EIN)
  tax_identifier_type VARCHAR(20), -- GST/VAT/EIN/TIN
  currency          CHAR(3) NOT NULL DEFAULT 'USD',
  payment_terms     VARCHAR(20) NOT NULL DEFAULT 'NET30',
  credit_limit      DECIMAL(20,4) NOT NULL DEFAULT 0,
  is_active         BOOLEAN NOT NULL DEFAULT TRUE,
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),
  created_by        UUID REFERENCES app_user(id),

  CONSTRAINT customer_currency_valid CHECK (char_length(currency) = 3),
  CONSTRAINT customer_payment_terms_valid CHECK (
    payment_terms IN ('NET15', 'NET30', 'NET45', 'NET60', 'NET90', 'IMMEDIATE')
  ),
  CONSTRAINT customer_credit_limit_positive CHECK (credit_limit >= 0)
);

CREATE INDEX idx_customer_tenant ON customer(tenant_id);
CREATE INDEX idx_customer_entity ON customer(entity_id);
CREATE INDEX idx_customer_name ON customer(tenant_id, name);

ALTER TABLE customer ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON customer
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE TRIGGER customer_audit
  AFTER INSERT OR UPDATE OR DELETE ON customer
  FOR EACH ROW EXECUTE FUNCTION write_audit_log();

-- ============================================================
-- TABLE: FX IMPORT JOB
-- Durable system job for fetching one provider FX rate batch.
-- Not tenant-facing.
-- ============================================================
CREATE TABLE fx_import_job (
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

CREATE INDEX idx_fx_import_job_claim
  ON fx_import_job(status, next_attempt_at, created_at);

COMMENT ON TABLE fx_import_job IS
  'Durable system job for fetching one provider FX batch. Not tenant-facing.';

-- ============================================================
-- TABLE: EXCHANGE RATE
-- Daily reference rates ingested from a provider (ECB), plus
-- manual overrides. Approved rates are immutable; corrections
-- supersede rather than mutate.
-- ============================================================
CREATE TABLE exchange_rate (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id               UUID NOT NULL REFERENCES tenant(id),
  from_currency           CHAR(3) NOT NULL,
  to_currency             CHAR(3) NOT NULL,
  rate                    DECIMAL(20,8) NOT NULL,
  effective_date          DATE NOT NULL,
  source                  VARCHAR(50) NOT NULL DEFAULT 'XE',  -- XE/Bloomberg/RBI/ECB/LEGACY
  rate_type               VARCHAR(30) NOT NULL DEFAULT 'DAILY_REFERENCE',
  status                  VARCHAR(20) NOT NULL DEFAULT 'PENDING',
  provider_effective_date DATE NOT NULL,
  fetched_at              TIMESTAMP NOT NULL,
  raw_quote_currency      CHAR(3),
  raw_quote_rate          DECIMAL(20,8),
  is_derived              BOOLEAN NOT NULL DEFAULT FALSE,
  raw_response_hash       CHAR(64),
  import_job_id           UUID REFERENCES fx_import_job(id),
  is_manual_override      BOOLEAN NOT NULL DEFAULT FALSE,
  approved_by             UUID REFERENCES app_user(id),
  approved_at             TIMESTAMP,
  supersedes_rate_id      UUID REFERENCES exchange_rate(id),
  created_at              TIMESTAMP NOT NULL DEFAULT NOW(),

  CONSTRAINT exchange_rate_positive CHECK (rate > 0),
  CONSTRAINT exchange_rate_status_valid CHECK (
    status IN ('PENDING', 'APPROVED', 'REJECTED', 'SUPERSEDED')
  ),
  CONSTRAINT exchange_rate_type_valid CHECK (
    rate_type IN ('DAILY_REFERENCE', 'MANUAL')
  ),
  CONSTRAINT exchange_rate_currency_pair_valid CHECK (
    from_currency ~ '^[A-Z]{3}$'
    AND to_currency ~ '^[A-Z]{3}$'
    AND from_currency <> to_currency
  ),
  CONSTRAINT exchange_rate_raw_quote_positive CHECK (
    raw_quote_rate IS NULL OR raw_quote_rate > 0
  ),
  CONSTRAINT exchange_rate_hash_valid CHECK (
    raw_response_hash IS NULL OR raw_response_hash ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT exchange_rate_approval_valid CHECK (
    status <> 'APPROVED' OR approved_at IS NOT NULL
  ),
  CONSTRAINT exchange_rate_manual_approval_valid CHECK (
    NOT is_manual_override OR status <> 'APPROVED' OR approved_by IS NOT NULL
  ),
  CONSTRAINT exchange_rate_supersedes_other CHECK (
    supersedes_rate_id IS NULL OR supersedes_rate_id <> id
  )
);

CREATE INDEX idx_exchange_rate_tenant ON exchange_rate(tenant_id);
CREATE INDEX idx_exchange_rate_lookup
  ON exchange_rate(tenant_id, from_currency, to_currency, rate_type, effective_date DESC);
CREATE UNIQUE INDEX uq_exchange_rate_approved
  ON exchange_rate(tenant_id, from_currency, to_currency, effective_date, rate_type)
  WHERE status = 'APPROVED';
CREATE UNIQUE INDEX uq_exchange_rate_import_pair
  ON exchange_rate(import_job_id, tenant_id, from_currency, to_currency)
  WHERE import_job_id IS NOT NULL;

ALTER TABLE exchange_rate ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON exchange_rate
  USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
  )
  WITH CHECK (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
  );

CREATE TRIGGER exchange_rate_audit
  AFTER INSERT OR UPDATE OR DELETE ON exchange_rate
  FOR EACH ROW EXECUTE FUNCTION write_audit_log();

-- ============================================================
-- TABLE: ACCOUNTING PERIOD
-- Month/year close tracking
-- ============================================================
CREATE TABLE accounting_period (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenant(id),
  entity_id         UUID NOT NULL REFERENCES entity(id),
  period_name       VARCHAR(50) NOT NULL,  -- "January 2024"
  start_date        DATE NOT NULL,
  end_date          DATE NOT NULL,
  status            VARCHAR(20) NOT NULL DEFAULT 'OPEN',
  closed_by         UUID REFERENCES app_user(id),
  closed_at         TIMESTAMP,
  locked_by         UUID REFERENCES app_user(id),
  locked_at         TIMESTAMP,
  reopened_by       UUID REFERENCES app_user(id),
  reopened_at       TIMESTAMP,
  reopen_reason     TEXT,
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),

  CONSTRAINT period_status_valid CHECK (
    status IN ('OPEN', 'CLOSED', 'LOCKED')
  ),
  CONSTRAINT period_dates_valid CHECK (start_date < end_date),
  CONSTRAINT period_state_metadata_valid CHECK (
    status = 'OPEN'
    OR (status = 'CLOSED' AND closed_by IS NOT NULL AND closed_at IS NOT NULL)
    OR (status = 'LOCKED' AND closed_by IS NOT NULL AND closed_at IS NOT NULL
        AND locked_by IS NOT NULL AND locked_at IS NOT NULL)
  ),
  CONSTRAINT period_unique UNIQUE (tenant_id, entity_id, start_date)
);

CREATE INDEX idx_period_tenant ON accounting_period(tenant_id);
CREATE INDEX idx_period_status ON accounting_period(tenant_id, entity_id, status);
CREATE INDEX idx_period_dates ON accounting_period(tenant_id, entity_id, start_date, end_date);

CREATE TRIGGER accounting_period_transition_check
  BEFORE UPDATE OF status ON accounting_period
  FOR EACH ROW EXECUTE FUNCTION check_period_transition();

-- ============================================================
-- TABLE: INVOICE
-- Heart of the system
-- ============================================================
CREATE TABLE invoice (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             UUID NOT NULL REFERENCES tenant(id),
  entity_id             UUID NOT NULL REFERENCES entity(id),
  customer_id           UUID NOT NULL REFERENCES customer(id),
  period_id             UUID REFERENCES accounting_period(id),
  po_reference          VARCHAR(100),
  status                VARCHAR(30) NOT NULL DEFAULT 'DRAFT',
  is_submitted          BOOLEAN NOT NULL DEFAULT FALSE,
  submitted_to          UUID REFERENCES app_user(id),
  submitted_at          TIMESTAMP,
  -- Currency
  transaction_currency  CHAR(3) NOT NULL,
  exchange_rate         DECIMAL(20,8) NOT NULL DEFAULT 1,
  exchange_rate_id      UUID REFERENCES exchange_rate(id),
  base_currency         CHAR(3) NOT NULL,
  -- Amounts (transaction currency)
  subtotal_amount       DECIMAL(20,4) NOT NULL DEFAULT 0,
  tax_amount            DECIMAL(20,4) NOT NULL DEFAULT 0,
  total_amount          DECIMAL(20,4) NOT NULL DEFAULT 0,
  -- Amounts (base/reporting currency)
  base_subtotal_amount  DECIMAL(20,4) NOT NULL DEFAULT 0,
  base_tax_amount       DECIMAL(20,4) NOT NULL DEFAULT 0,
  base_total_amount     DECIMAL(20,4) NOT NULL DEFAULT 0,
  -- Outstanding balance
  balance_amount        DECIMAL(20,4) NOT NULL DEFAULT 0,
  base_balance_amount   DECIMAL(20,4) NOT NULL DEFAULT 0,
  -- Dates
  invoice_date          DATE NOT NULL DEFAULT CURRENT_DATE,
  due_date              DATE NOT NULL,
  payment_terms         VARCHAR(20) NOT NULL DEFAULT 'NET30',
  -- Intercompany
  is_intercompany       BOOLEAN NOT NULL DEFAULT FALSE,
  receiver_entity_id    UUID REFERENCES entity(id),
  -- Audit
  created_by            UUID NOT NULL REFERENCES app_user(id),
  approved_by           UUID REFERENCES app_user(id),
  approved_at           TIMESTAMP,
  rejection_reason      TEXT,
  sent_at               TIMESTAMP,
  version               INTEGER NOT NULL DEFAULT 1,  -- optimistic locking (ABA prevention)
  created_at            TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMP NOT NULL DEFAULT NOW(),

  CONSTRAINT invoice_status_valid CHECK (
    status IN ('DRAFT','APPROVED','SENT','PARTIALLY_PAID','PAID','VOID','WRITTEN_OFF')
  ),
  CONSTRAINT invoice_currency_valid CHECK (char_length(transaction_currency) = 3),
  CONSTRAINT invoice_total_valid CHECK (
    total_amount = subtotal_amount + tax_amount
  ),
  CONSTRAINT invoice_balance_valid CHECK (
    balance_amount >= 0 AND balance_amount <= total_amount
  ),
  CONSTRAINT invoice_base_balance_valid CHECK (
    base_balance_amount >= 0 AND base_balance_amount <= base_total_amount
  ),
  CONSTRAINT invoice_due_date_valid CHECK (due_date >= invoice_date),
  -- SOX: creator and approver must be different
  CONSTRAINT invoice_sox_segregation CHECK (
    approved_by IS NULL OR created_by != approved_by
  ),
  -- Intercompany: receiver entity required if intercompany
  CONSTRAINT invoice_intercompany_valid CHECK (
    (is_intercompany = FALSE) OR
    (is_intercompany = TRUE AND receiver_entity_id IS NOT NULL)
  ),
  -- FX: transaction currency == base currency implies rate 1 and no rate
  -- reference; a differing currency requires a locked exchange_rate_id
  CONSTRAINT invoice_fx_rate_reference_valid CHECK (
    (transaction_currency = base_currency AND exchange_rate = 1)
    OR
    (transaction_currency <> base_currency AND exchange_rate_id IS NOT NULL)
  )
);

CREATE INDEX idx_invoice_tenant ON invoice(tenant_id);
CREATE INDEX idx_invoice_entity ON invoice(entity_id);
CREATE INDEX idx_invoice_customer ON invoice(customer_id);
CREATE INDEX idx_invoice_status ON invoice(tenant_id, status);
CREATE INDEX idx_invoice_due_date ON invoice(tenant_id, due_date);
CREATE INDEX idx_invoice_date ON invoice(tenant_id, invoice_date);
CREATE INDEX idx_invoice_exchange_rate
  ON invoice(exchange_rate_id)
  WHERE exchange_rate_id IS NOT NULL;

ALTER TABLE invoice ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON invoice
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE TRIGGER invoice_period_check
  BEFORE UPDATE OF status ON invoice
  FOR EACH ROW EXECUTE FUNCTION check_period_open();

CREATE TRIGGER invoice_audit
  AFTER INSERT OR UPDATE OR DELETE ON invoice
  FOR EACH ROW EXECUTE FUNCTION write_audit_log();

COMMENT ON COLUMN invoice.exchange_rate IS
  'Rate locked on invoice_date. FX gain/loss calculated on payment vs this rate.';

COMMENT ON COLUMN invoice.is_intercompany IS
  'TRUE when sender and receiver are both entities within same tenant. Eliminated in consolidated reports.';

-- ============================================================
-- TABLE: INVOICE LINE ITEM
-- Individual lines on an invoice
-- ============================================================
CREATE TABLE invoice_line_item (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenant(id),
  invoice_id        UUID NOT NULL REFERENCES invoice(id) ON DELETE CASCADE,
  line_number       INTEGER NOT NULL,
  description       VARCHAR(500) NOT NULL,
  quantity          DECIMAL(20,4) NOT NULL,
  unit_price        DECIMAL(20,4) NOT NULL,
  subtotal          DECIMAL(20,4) NOT NULL,
  tax_rate          DECIMAL(8,4) NOT NULL DEFAULT 0,
  tax_jurisdiction  VARCHAR(20),  -- MH/KA/IGST/VAT etc
  tax_amount        DECIMAL(20,4) NOT NULL DEFAULT 0,
  total_price       DECIMAL(20,4) NOT NULL,
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),

  CONSTRAINT line_item_quantity_positive CHECK (quantity > 0),
  CONSTRAINT line_item_unit_price_positive CHECK (unit_price >= 0),
  CONSTRAINT line_item_tax_rate_valid CHECK (tax_rate >= 0 AND tax_rate <= 100),
  CONSTRAINT line_item_total_valid CHECK (
    total_price = subtotal + tax_amount
  ),
  CONSTRAINT line_item_subtotal_valid CHECK (
    subtotal = quantity * unit_price
  ),
  CONSTRAINT line_item_line_number_unique UNIQUE (invoice_id, line_number)
);

CREATE INDEX idx_line_item_invoice ON invoice_line_item(invoice_id);
CREATE INDEX idx_line_item_tenant ON invoice_line_item(tenant_id);

ALTER TABLE invoice_line_item ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON invoice_line_item
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- ============================================================
-- TABLE: PAYMENT
-- Money received from customer
-- ============================================================
CREATE TABLE payment (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             UUID NOT NULL REFERENCES tenant(id),
  entity_id             UUID NOT NULL REFERENCES entity(id),
  customer_id           UUID NOT NULL REFERENCES customer(id),
  payment_reference     VARCHAR(100) NOT NULL,  -- UTR/cheque number
  payment_date          DATE NOT NULL DEFAULT CURRENT_DATE,
  -- Currency
  transaction_currency  CHAR(3) NOT NULL,
  exchange_rate         DECIMAL(20,8) NOT NULL DEFAULT 1,
  exchange_rate_id      UUID REFERENCES exchange_rate(id),
  base_currency         CHAR(3) NOT NULL,
  -- Amounts
  amount                DECIMAL(20,4) NOT NULL,
  base_amount           DECIMAL(20,4) NOT NULL,
  allocated_amount      DECIMAL(20,4) NOT NULL DEFAULT 0,
  unallocated_amount    DECIMAL(20,4) NOT NULL,
  base_allocated_amount   DECIMAL(20,4) NOT NULL DEFAULT 0,
  base_unallocated_amount DECIMAL(20,4) NOT NULL DEFAULT 0,
  -- Payment details
  payment_method        VARCHAR(50) NOT NULL,  -- NEFT/RTGS/SWIFT/CHEQUE
  status                VARCHAR(30) NOT NULL DEFAULT 'PENDING',
  allocation_mode       VARCHAR(20) NOT NULL DEFAULT 'AUTO',  -- AUTO/MANUAL
  -- Idempotency
  idempotency_key       VARCHAR(255),
  -- Audit
  created_by            UUID NOT NULL REFERENCES app_user(id),
  created_at            TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMP NOT NULL DEFAULT NOW(),

  CONSTRAINT payment_amount_positive CHECK (amount > 0),
  CONSTRAINT payment_status_valid CHECK (
    status IN ('PENDING', 'APPLIED', 'PARTIALLY_APPLIED', 'FAILED', 'REVERSED')
  ),
  CONSTRAINT payment_method_valid CHECK (
    payment_method IN ('NEFT', 'RTGS', 'SWIFT', 'CHEQUE', 'CARD', 'UPI', 'ACH')
  ),
  CONSTRAINT payment_allocation_mode_valid CHECK (
    allocation_mode IN ('AUTO', 'MANUAL')
  ),
  CONSTRAINT payment_unallocated_valid CHECK (
    unallocated_amount = amount - allocated_amount
  ),
  CONSTRAINT payment_base_allocation_valid CHECK (
    base_allocated_amount >= 0
    AND base_unallocated_amount >= 0
    AND base_amount = base_allocated_amount + base_unallocated_amount
  ),
  CONSTRAINT payment_currency_valid CHECK (char_length(transaction_currency) = 3),
  CONSTRAINT payment_fx_rate_reference_valid CHECK (
    (transaction_currency = base_currency AND exchange_rate = 1)
    OR
    (transaction_currency <> base_currency AND exchange_rate_id IS NOT NULL)
  ),
  CONSTRAINT payment_reference_unique UNIQUE (tenant_id, customer_id, payment_reference),
  CONSTRAINT payment_idempotency_unique UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX idx_payment_tenant ON payment(tenant_id);
CREATE INDEX idx_payment_customer ON payment(customer_id);
CREATE INDEX idx_payment_date ON payment(tenant_id, payment_date);
CREATE INDEX idx_payment_reference ON payment(tenant_id, payment_reference);
CREATE INDEX idx_payment_exchange_rate
  ON payment(exchange_rate_id)
  WHERE exchange_rate_id IS NOT NULL;

ALTER TABLE payment ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON payment
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE TRIGGER payment_audit
  AFTER INSERT OR UPDATE OR DELETE ON payment
  FOR EACH ROW EXECUTE FUNCTION write_audit_log();

COMMENT ON COLUMN payment.idempotency_key IS
  'Client-supplied key to prevent duplicate payment processing on retry.';

-- ============================================================
-- TABLE: PAYMENT ALLOCATION
-- Bridge table: links payment to invoices. Many payments -> many invoices.
-- ============================================================
CREATE TABLE payment_allocation (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenant(id),
  payment_id        UUID NOT NULL REFERENCES payment(id),
  invoice_id        UUID NOT NULL REFERENCES invoice(id),
  amount_allocated  DECIMAL(20,4) NOT NULL,
  -- FX gain/loss (payment rate vs invoice rate), realized in base currency
  fx_gain_loss      DECIMAL(20,4) NOT NULL DEFAULT 0,
  base_payment_amount DECIMAL(20,4) NOT NULL DEFAULT 0,
  base_ar_amount      DECIMAL(20,4) NOT NULL DEFAULT 0,
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),
  created_by        UUID NOT NULL REFERENCES app_user(id),

  CONSTRAINT allocation_amount_positive CHECK (amount_allocated > 0),
  CONSTRAINT allocation_unique UNIQUE (payment_id, invoice_id),
  CONSTRAINT allocation_base_amounts_valid CHECK (
    base_payment_amount > 0 AND base_ar_amount > 0
  ),
  CONSTRAINT allocation_realized_fx_valid CHECK (
    fx_gain_loss = base_payment_amount - base_ar_amount
  )
);

CREATE INDEX idx_allocation_payment ON payment_allocation(payment_id);
CREATE INDEX idx_allocation_invoice ON payment_allocation(invoice_id);
CREATE INDEX idx_allocation_tenant ON payment_allocation(tenant_id);

ALTER TABLE payment_allocation ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON payment_allocation
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- ============================================================
-- TABLE: JOURNAL ENTRY
-- Immutable accounting record. Polymorphic reference
-- (reference_type + reference_id).
-- ============================================================
CREATE TABLE journal_entry (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenant(id),
  entity_id         UUID NOT NULL REFERENCES entity(id),
  period_id         UUID NOT NULL REFERENCES accounting_period(id),
  reference_type    VARCHAR(50) NOT NULL,
  reference_id      UUID NOT NULL,
  document_date     DATE NOT NULL,
  entry_date        DATE NOT NULL DEFAULT CURRENT_DATE, -- GL posting date
  adjusts_period_id UUID REFERENCES accounting_period(id),
  adjustment_reason TEXT,
  description       TEXT NOT NULL,
  currency          CHAR(3) NOT NULL,
  is_reversed       BOOLEAN NOT NULL DEFAULT FALSE,
  reversed_by       UUID REFERENCES journal_entry(id),
  created_by        UUID NOT NULL REFERENCES app_user(id),
  approved_by       UUID REFERENCES app_user(id),
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),

  CONSTRAINT je_reference_type_valid CHECK (
    reference_type IN ('INVOICE', 'PAYMENT', 'CREDIT_MEMO', 'WRITE_OFF', 'VOID', 'MANUAL', 'PRIOR_PERIOD_ADJUSTMENT', 'FX_REVALUATION')
  ),
  CONSTRAINT je_prior_period_adjustment_valid CHECK (
    reference_type <> 'PRIOR_PERIOD_ADJUSTMENT'
    OR (adjusts_period_id IS NOT NULL
        AND NULLIF(BTRIM(adjustment_reason), '') IS NOT NULL
        AND approved_by IS NOT NULL
        AND created_by <> approved_by)
  )
  -- NOTE: No UPDATE/DELETE allowed. Immutable by application policy.
  -- Corrections via reversing journal entries only.
);

CREATE INDEX idx_je_tenant ON journal_entry(tenant_id);
CREATE INDEX idx_je_entity ON journal_entry(entity_id);
CREATE INDEX idx_je_reference ON journal_entry(reference_type, reference_id);
CREATE INDEX idx_je_date ON journal_entry(tenant_id, entry_date);

ALTER TABLE journal_entry ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON journal_entry
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE TRIGGER journal_entry_period_check
  BEFORE INSERT ON journal_entry
  FOR EACH ROW EXECUTE FUNCTION check_journal_period_open();

COMMENT ON TABLE journal_entry IS
  'Immutable. No UPDATE/DELETE ever. Corrections via reversing entries only. SOX requirement.';

-- ============================================================
-- TABLE: JOURNAL ENTRY LINE
-- Individual debit/credit lines. Debits must equal credits per
-- journal entry, in both transaction and base currency.
-- ============================================================
CREATE TABLE journal_entry_line (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenant(id),
  journal_entry_id  UUID NOT NULL REFERENCES journal_entry(id),
  gl_account_id     UUID NOT NULL REFERENCES gl_account(id),
  description       VARCHAR(500),
  debit_amount      DECIMAL(20,4) NOT NULL DEFAULT 0,
  credit_amount     DECIMAL(20,4) NOT NULL DEFAULT 0,
  base_debit_amount DECIMAL(20,4) NOT NULL DEFAULT 0,
  base_credit_amount DECIMAL(20,4) NOT NULL DEFAULT 0,
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),

  -- One of debit or credit must be zero (double entry). Realized FX exists
  -- only in base currency, so a line may also be zero on both transaction
  -- sides as long as exactly one base-currency side is positive.
  CONSTRAINT je_line_double_entry CHECK (
    (debit_amount = 0 AND credit_amount > 0)
    OR (credit_amount = 0 AND debit_amount > 0)
    OR (
      debit_amount = 0
      AND credit_amount = 0
      AND (
        (base_debit_amount = 0 AND base_credit_amount > 0)
        OR (base_credit_amount = 0 AND base_debit_amount > 0)
      )
    )
  ),
  CONSTRAINT je_line_base_double_entry CHECK (
    (base_debit_amount = 0 AND base_credit_amount > 0)
    OR (base_credit_amount = 0 AND base_debit_amount > 0)
  ),
  CONSTRAINT je_line_amounts_positive CHECK (
    debit_amount >= 0 AND credit_amount >= 0
  ),
  CONSTRAINT je_line_base_amounts_positive CHECK (
    base_debit_amount >= 0 AND base_credit_amount >= 0
  )
);

CREATE INDEX idx_je_line_entry ON journal_entry_line(journal_entry_id);
CREATE INDEX idx_je_line_account ON journal_entry_line(gl_account_id);
CREATE INDEX idx_je_line_tenant ON journal_entry_line(tenant_id);

ALTER TABLE journal_entry_line ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON journal_entry_line
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- ============================================================
-- TABLE: CREDIT MEMO
-- Correction document against an approved/sent/paid invoice
-- ============================================================
CREATE TABLE credit_memo (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenant(id),
  entity_id         UUID NOT NULL REFERENCES entity(id),
  invoice_id        UUID NOT NULL REFERENCES invoice(id),
  reason_code       VARCHAR(50) NOT NULL,
  description       TEXT,
  amount            DECIMAL(20,4) NOT NULL,
  base_amount       DECIMAL(20,4) NOT NULL,
  currency          CHAR(3) NOT NULL,
  status            VARCHAR(30) NOT NULL DEFAULT 'DRAFT',
  replacement_invoice_id UUID REFERENCES invoice(id),
  created_by        UUID NOT NULL REFERENCES app_user(id),
  approved_by       UUID REFERENCES app_user(id),
  approved_at       TIMESTAMP,
  applied_at        TIMESTAMP,
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),

  CONSTRAINT cm_reason_valid CHECK (
    reason_code IN ('OVERCHARGE', 'RETURN', 'DUPLICATE', 'CANCEL', 'OTHER')
  ),
  CONSTRAINT cm_status_valid CHECK (
    status IN ('DRAFT', 'APPROVED', 'APPLIED', 'VOID')
  ),
  CONSTRAINT cm_amount_positive CHECK (amount > 0),
  -- SOX: creator and approver must be different
  CONSTRAINT cm_sox_segregation CHECK (
    approved_by IS NULL OR created_by != approved_by
  )
);

CREATE INDEX idx_cm_tenant ON credit_memo(tenant_id);
CREATE INDEX idx_cm_invoice ON credit_memo(invoice_id);
CREATE INDEX idx_cm_status ON credit_memo(tenant_id, status);

ALTER TABLE credit_memo ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON credit_memo
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE TRIGGER credit_memo_audit
  AFTER INSERT OR UPDATE OR DELETE ON credit_memo
  FOR EACH ROW EXECUTE FUNCTION write_audit_log();

-- ============================================================
-- TABLE: DELIVERY OUTBOX
-- Durable invoice-delivery events committed atomically with
-- approval. Transactional outbox -> SQS -> consumer pipeline.
-- ============================================================
CREATE TABLE delivery_outbox (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenant(id),
  entity_id         UUID NOT NULL REFERENCES entity(id),
  invoice_id        UUID NOT NULL REFERENCES invoice(id),
  event_type        VARCHAR(50) NOT NULL,
  payload           JSONB NOT NULL,
  status            VARCHAR(20) NOT NULL DEFAULT 'PENDING',
  attempt_count     INTEGER NOT NULL DEFAULT 0,
  max_attempts      INTEGER NOT NULL DEFAULT 3,
  next_attempt_at   TIMESTAMP NOT NULL DEFAULT NOW(),
  locked_at         TIMESTAMP,
  delivered_at      TIMESTAMP,
  last_error        TEXT,
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMP NOT NULL DEFAULT NOW(),
  -- SQS publication lifecycle
  publish_attempt_count INTEGER NOT NULL DEFAULT 0,
  max_publish_attempts  INTEGER NOT NULL DEFAULT 10,
  sqs_message_id        VARCHAR(100),
  published_at          TIMESTAMP,

  CONSTRAINT delivery_outbox_event_unique UNIQUE (invoice_id, event_type),
  CONSTRAINT delivery_outbox_status_valid CHECK (
    status IN (
      'PENDING', 'PROCESSING', 'PUBLISHED', 'DELIVERING',
      'DELIVERED', 'DEAD'
    )
  ),
  CONSTRAINT delivery_outbox_attempts_valid CHECK (
    attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts
  ),
  CONSTRAINT delivery_outbox_publish_attempts_valid CHECK (
    publish_attempt_count >= 0
    AND max_publish_attempts > 0
    AND publish_attempt_count <= max_publish_attempts
  )
);

CREATE INDEX idx_delivery_outbox_claim
  ON delivery_outbox(status, next_attempt_at, created_at);
CREATE INDEX idx_delivery_outbox_tenant
  ON delivery_outbox(tenant_id);
CREATE INDEX idx_delivery_outbox_invoice
  ON delivery_outbox(tenant_id, entity_id, invoice_id, created_at);

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

COMMENT ON COLUMN delivery_outbox.sqs_message_id IS
  'Latest SQS message ID; duplicate publishes remain safe via the outbox event ID.';

-- ============================================================
-- TABLE: AUDIT LOG
-- Immutable SOX compliance trail. Written by DB triggers —
-- cannot be bypassed by the application.
-- ============================================================
CREATE TABLE audit_log (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID,  -- nullable: system events may not have tenant
  table_name        VARCHAR(100) NOT NULL,
  record_id         UUID NOT NULL,
  action            VARCHAR(20) NOT NULL,  -- INSERT/UPDATE/DELETE
  old_value         JSONB,
  new_value         JSONB,
  changed_by        UUID,  -- nullable: system/trigger changes
  changed_at        TIMESTAMP NOT NULL DEFAULT NOW(),
  ip_address        INET,
  session_id        VARCHAR(255),

  CONSTRAINT audit_action_valid CHECK (
    action IN ('INSERT', 'UPDATE', 'DELETE')
  )
  -- NOTE: No UPDATE/DELETE/RLS on this table.
  -- Immutable by design. SOX requirement.
  -- Archived to S3 WORM after 6 months.
);

CREATE INDEX idx_audit_tenant ON audit_log(tenant_id);
CREATE INDEX idx_audit_table_record ON audit_log(table_name, record_id);
CREATE INDEX idx_audit_changed_at ON audit_log(changed_at DESC);
CREATE INDEX idx_audit_changed_by ON audit_log(changed_by);

COMMENT ON TABLE audit_log IS
  'Immutable. Written by DB triggers. Archived to S3 WORM after 6 months. 7 year retention per SOX.';

-- ============================================================
-- TABLE: IDEMPOTENCY KEY
-- Prevents duplicate processing for all synchronous write APIs.
-- Scoped by entity as well as tenant so two entities in the same
-- tenant cannot collide on a client-generated key.
-- ============================================================
CREATE TABLE idempotency_key (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenant(id),
  entity_id         UUID NOT NULL REFERENCES entity(id),
  endpoint          VARCHAR(100) NOT NULL,
  key               VARCHAR(255) NOT NULL,
  request_hash      VARCHAR(64) NOT NULL,
  status            VARCHAR(20) NOT NULL DEFAULT 'PROCESSING',
  response_status   SMALLINT,
  response_body     JSONB,
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMP NOT NULL DEFAULT NOW(),
  expires_at        TIMESTAMP NOT NULL DEFAULT NOW() + INTERVAL '24 hours',

  CONSTRAINT idempotency_scope_unique UNIQUE (tenant_id, entity_id, endpoint, key),
  CONSTRAINT idempotency_request_hash_valid CHECK (request_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT idempotency_status_valid CHECK (
    status IN ('PROCESSING', 'COMPLETED')
  ),
  CONSTRAINT idempotency_response_status_valid CHECK (
    response_status IS NULL OR response_status BETWEEN 100 AND 599
  ),
  CONSTRAINT idempotency_completion_valid CHECK (
    (status = 'PROCESSING' AND response_status IS NULL AND response_body IS NULL)
    OR (status = 'COMPLETED' AND response_status IS NOT NULL AND response_body IS NOT NULL)
  ),
  CONSTRAINT idempotency_expiry_valid CHECK (
    expires_at > created_at
  )
);

CREATE INDEX idx_idempotency_tenant ON idempotency_key(tenant_id);
CREATE INDEX idx_idempotency_entity ON idempotency_key(entity_id);
CREATE INDEX idx_idempotency_expires ON idempotency_key(expires_at);

ALTER TABLE idempotency_key ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON idempotency_key
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- ============================================================
-- MATERIALIZED VIEW: AR AGING
-- Pre-computed for performance (refreshed every 5 minutes by
-- pg_cron — see 003_setup_pg_cron.sh). Base-currency subledger
-- report. DRAFT invoices have not posted to AR and must not
-- appear; PAID/VOID/WRITTEN_OFF have no open receivable.
-- "as_of" is the refresh timestamp so users know staleness; it
-- is not a caller-selected historical reporting date.
-- ============================================================
CREATE MATERIALIZED VIEW ar_aging AS
SELECT
  i.tenant_id,
  i.entity_id,
  i.customer_id,
  c.name AS customer_name,
  NOW() AS as_of,
  COUNT(*) FILTER (
    WHERE i.due_date >= CURRENT_DATE
      AND i.status IN ('APPROVED', 'SENT', 'PARTIALLY_PAID')
  ) AS current_count,
  COALESCE(SUM(i.base_balance_amount) FILTER (
    WHERE i.due_date >= CURRENT_DATE
      AND i.status IN ('APPROVED', 'SENT', 'PARTIALLY_PAID')
  ), 0) AS current_amount,
  COUNT(*) FILTER (
    WHERE i.due_date < CURRENT_DATE
      AND i.due_date >= CURRENT_DATE - INTERVAL '30 days'
      AND i.status IN ('APPROVED', 'SENT', 'PARTIALLY_PAID')
  ) AS days_30_count,
  COALESCE(SUM(i.base_balance_amount) FILTER (
    WHERE i.due_date < CURRENT_DATE
      AND i.due_date >= CURRENT_DATE - INTERVAL '30 days'
      AND i.status IN ('APPROVED', 'SENT', 'PARTIALLY_PAID')
  ), 0) AS days_30_amount,
  COUNT(*) FILTER (
    WHERE i.due_date < CURRENT_DATE - INTERVAL '30 days'
      AND i.due_date >= CURRENT_DATE - INTERVAL '60 days'
      AND i.status IN ('APPROVED', 'SENT', 'PARTIALLY_PAID')
  ) AS days_60_count,
  COALESCE(SUM(i.base_balance_amount) FILTER (
    WHERE i.due_date < CURRENT_DATE - INTERVAL '30 days'
      AND i.due_date >= CURRENT_DATE - INTERVAL '60 days'
      AND i.status IN ('APPROVED', 'SENT', 'PARTIALLY_PAID')
  ), 0) AS days_60_amount,
  COUNT(*) FILTER (
    WHERE i.due_date < CURRENT_DATE - INTERVAL '60 days'
      AND i.status IN ('APPROVED', 'SENT', 'PARTIALLY_PAID')
  ) AS days_90_plus_count,
  COALESCE(SUM(i.base_balance_amount) FILTER (
    WHERE i.due_date < CURRENT_DATE - INTERVAL '60 days'
      AND i.status IN ('APPROVED', 'SENT', 'PARTIALLY_PAID')
  ), 0) AS days_90_plus_amount,
  COALESCE(SUM(i.base_balance_amount) FILTER (
    WHERE i.status IN ('APPROVED', 'SENT', 'PARTIALLY_PAID')
  ), 0) AS total_outstanding
FROM invoice i
JOIN customer c ON c.id = i.customer_id
GROUP BY i.tenant_id, i.entity_id, i.customer_id, c.name;

CREATE UNIQUE INDEX idx_ar_aging_pk ON ar_aging(tenant_id, entity_id, customer_id);
CREATE INDEX idx_ar_aging_tenant ON ar_aging(tenant_id);

-- ============================================================
-- DEFAULT CHART OF ACCOUNTS
-- Standard account codes seeded on tenant creation
-- ============================================================
-- These are inserted by application code on tenant setup:
-- 1100 Cash
-- 1200 Accounts Receivable
-- 1300 Inventory
-- 2100 Customer Credit (overpayment liability)
-- 2200 Tax Payable
-- 3100 Sales Revenue
-- 3200 Deferred Revenue
-- 4100 Bad Debt Expense
-- 4200 FX Gain/Loss
