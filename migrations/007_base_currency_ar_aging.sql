-- AR aging is a base-currency subledger report. DRAFT invoices have not posted
-- to AR and must not appear; PAID/VOID/WRITTEN_OFF have no open receivable.
DROP MATERIALIZED VIEW IF EXISTS ar_aging;

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

CREATE UNIQUE INDEX idx_ar_aging_pk
  ON ar_aging(tenant_id, entity_id, customer_id);
CREATE INDEX idx_ar_aging_tenant ON ar_aging(tenant_id);
