#!/bin/bash
# Delete all data created by debug sessions (0_create_tenant.sh,
# 1_create_invoice.sh, etc), scoped strictly to tenants named "Debug Tenant%"
# (0_create_tenant.sh / 0_create_entity.sh) or "Debug Bootstrap Tenant"
# (setup_bootstrap_admin.py). Leaves every other tenant and all its data
# untouched.
#
# Deliberately does NOT match on customer name ("Debug Customer") anymore --
# the main seed tenant ("Reliance", 00000000-0000-0000-0000-000000000001,
# used by FX/credit-memo/payment-concurrency test scenarios) happens to also
# have a customer named "Debug Customer" under it, so that match could
# cascade-delete a shared, non-debug tenant. Name-based tenant matching only.
# Usage: ./reset_debug_data.sh

PROJECT=/Users/pankajkapoor/projects/erp-ar-module

cd "$PROJECT" || exit 1

echo ""
echo "=== Resetting debug session data ==="
echo ""

docker compose exec -T db psql -U erp_user -d erp_db -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;

CREATE TEMP TABLE debug_tenant_ids AS
SELECT id AS tenant_id FROM tenant WHERE name LIKE 'Debug Tenant%' OR name = 'Debug Bootstrap Tenant';

DELETE FROM payment_allocation   WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM payment              WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM journal_entry_line   WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM journal_entry        WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM invoice_line_item    WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM credit_memo          WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM delivery_outbox      WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM idempotency_key      WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM invoice              WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM accounting_period    WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM gl_account           WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM customer             WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM app_user             WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM entity               WHERE tenant_id IN (SELECT tenant_id FROM debug_tenant_ids);
DELETE FROM tenant               WHERE id        IN (SELECT tenant_id FROM debug_tenant_ids);

SELECT count(*) AS debug_tenants_cleaned FROM debug_tenant_ids;

COMMIT;
SQL

echo ""
echo "=== Done ==="
