#!/bin/bash
# Delete all data created by debug sessions (1_create_invoice.sh etc), scoped
# strictly to tenants that have a customer named "Debug Customer" -- the name
# setup_debug_session.py always uses. Leaves every other tenant and all
# gl_account seed data untouched.
# Usage: ./reset_debug_data.sh

PROJECT=/Users/pankajkapoor/projects/erp-ar-module

cd "$PROJECT" || exit 1

echo ""
echo "=== Resetting debug session data ==="
echo ""

docker compose exec -T db psql -U erp_user -d erp_db -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;

CREATE TEMP TABLE debug_tenant_ids AS
SELECT DISTINCT tenant_id
FROM customer
WHERE name = 'Debug Customer';

DELETE FROM payment_allocation   WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM payment              WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM journal_entry_line   WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM journal_entry        WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM invoice_line_item    WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM credit_memo          WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM delivery_outbox      WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM idempotency_key      WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM invoice              WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);

SELECT count(*) AS debug_tenants_cleaned FROM debug_tenant_ids;

COMMIT;
SQL

echo ""
echo "=== Done ==="
