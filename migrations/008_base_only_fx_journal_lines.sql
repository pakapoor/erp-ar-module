-- Realized FX exists only in the entity's base currency. Permit a journal line
-- with zero transaction debit/credit when exactly one base-currency side is
-- positive, while retaining strict double-entry shape in both representations.
BEGIN;

ALTER TABLE journal_entry_line
  DROP CONSTRAINT IF EXISTS je_line_double_entry;

ALTER TABLE journal_entry_line
  ADD CONSTRAINT je_line_double_entry CHECK (
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
  ADD CONSTRAINT je_line_base_double_entry CHECK (
    (base_debit_amount = 0 AND base_credit_amount > 0)
    OR (base_credit_amount = 0 AND base_debit_amount > 0)
  ),
  ADD CONSTRAINT je_line_base_amounts_positive CHECK (
    base_debit_amount >= 0 AND base_credit_amount >= 0
  );

COMMIT;
