#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

BASE_URL="${BASE_URL:-http://localhost:8000}"
TENANT_ID="00000000-0000-0000-0000-000000000001"
ENTITY_ID="00000000-0000-0000-0000-000000000002"
USER_ID="00000000-0000-0000-0000-000000000003"
CUSTOMER_ID="00000000-0000-0000-0000-000000000005"
PO_REFERENCE="PO-DEBUG-INVOICE-WALKTHROUGH"

cleanup_previous_draft() {
  local existing_status

  existing_status="$(docker compose exec -T db psql -U erp_user -d erp_db -At \
    -v po_reference="$PO_REFERENCE" \
    -v tenant_id="$TENANT_ID" \
    -v entity_id="$ENTITY_ID" \
    -c "SELECT status
        FROM invoice
        WHERE po_reference = :'po_reference'
          AND tenant_id = :'tenant_id'::uuid
          AND entity_id = :'entity_id'::uuid
        ORDER BY created_at DESC
        LIMIT 1;")"

  if [[ -z "$existing_status" ]]; then
    echo "No previous debug invoice found."
    return
  fi

  if [[ "$existing_status" != "DRAFT" ]]; then
    echo "Refusing to delete existing $PO_REFERENCE invoice: status is $existing_status, not DRAFT." >&2
    echo "Use a new PO reference or void the financial document through the API." >&2
    exit 1
  fi

  docker compose exec -T db psql -U erp_user -d erp_db -v ON_ERROR_STOP=1 \
    -v po_reference="$PO_REFERENCE" \
    -v tenant_id="$TENANT_ID" \
    -v entity_id="$ENTITY_ID" <<'SQL'
BEGIN;
DELETE FROM invoice
WHERE po_reference = :'po_reference'
  AND tenant_id = :'tenant_id'::uuid
  AND entity_id = :'entity_id'::uuid
  AND status = 'DRAFT';
COMMIT;
SQL

  echo "Deleted the previous DRAFT debug invoice and its cascading line items."
}

if ! docker compose ps --status running --services | grep -qx app; then
  echo "The Docker app service is not running. Start the debug stack first." >&2
  exit 1
fi

cleanup_previous_draft

TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token(
    user_id='$USER_ID',
    tenant_id='$TENANT_ID',
    entity_id='$ENTITY_ID',
    roles=['invoice_creator'],
))
")"

IDEMPOTENCY_KEY="$(uuidgen | tr '[:upper:]' '[:lower:]')"

echo "Sending one POST /api/v1/invoices request with idempotency key: $IDEMPOTENCY_KEY"
echo "VS Code should stop at your breakpoint now."

curl --fail-with-body --silent --show-error \
  -X POST "$BASE_URL/api/v1/invoices" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Idempotency-Key: $IDEMPOTENCY_KEY" \
  -d "{
    \"customer_id\": \"$CUSTOMER_ID\",
    \"po_reference\": \"$PO_REFERENCE\",
    \"invoice_date\": \"2026-07-21\",
    \"payment_terms\": \"NET30\",
    \"currency\": \"INR\",
    \"line_items\": [
      {
        \"description\": \"Debugger walk-through item\",
        \"quantity\": 2,
        \"unit_price\": 50000,
        \"tax_rate\": 18,
        \"tax_jurisdiction\": \"MH\"
      }
    ]
  }" | python3 -m json.tool
