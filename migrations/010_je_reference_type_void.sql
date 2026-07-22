-- Voiding an APPROVED/SENT invoice reverses its GL entry with reference_type
-- 'VOID' (see credit_memos.py void endpoint), but that value was missing from
-- the allowed set, causing every such void to fail with a check violation.
BEGIN;

ALTER TABLE journal_entry
  DROP CONSTRAINT IF EXISTS je_reference_type_valid;

ALTER TABLE journal_entry
  ADD CONSTRAINT je_reference_type_valid CHECK (
    reference_type IN ('INVOICE', 'PAYMENT', 'CREDIT_MEMO', 'WRITE_OFF', 'VOID', 'MANUAL', 'PRIOR_PERIOD_ADJUSTMENT', 'FX_REVALUATION')
  );

COMMIT;
