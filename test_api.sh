#!/bin/bash

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
TENANT_ID="00000000-0000-0000-0000-000000000001"
ENTITY_ID="00000000-0000-0000-0000-000000000002"
RAHUL_ID="00000000-0000-0000-0000-000000000003"
PRIYA_ID="00000000-0000-0000-0000-000000000004"
CUSTOMER_ID="00000000-0000-0000-0000-000000000005"

pretty_print() {
  python3 -m json.tool <<< "$1"
}

assert_json() {
  JSON_RESPONSE="$1" ASSERT_EXPR="$2" ASSERT_LABEL="$3" python3 -c '
import json
import os

data = json.loads(os.environ["JSON_RESPONSE"])
label = os.environ["ASSERT_LABEL"]
if not eval(os.environ["ASSERT_EXPR"], {"data": data}):
    raise SystemExit("FAIL: " + label)
print("PASS: " + label)
'
}

expect_status() {
  expected="$1"
  label="$2"
  shift 2
  actual="$(curl --silent --show-error --output /dev/null --write-out "%{http_code}" "$@")"
  if [ "$actual" != "$expected" ]; then
    echo "FAIL: $label (expected HTTP $expected, got $actual)" >&2
    exit 1
  fi
  echo "PASS: $label (HTTP $actual)"
}

echo "=== Setup: seed data and runtime JWTs ==="
docker compose exec -T app python -m src.seed_data >/dev/null

TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token(
    user_id='$RAHUL_ID',
    tenant_id='$TENANT_ID',
    entity_id='$ENTITY_ID',
    roles=['invoice_creator'],
))
")"

PRIYA_TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token(
    user_id='$PRIYA_ID',
    tenant_id='$TENANT_ID',
    entity_id='$ENTITY_ID',
    roles=['invoice_approver', 'cfo'],
))
")"

OTHER_TENANT_TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token(
    user_id='00000000-0000-0000-0000-000000000097',
    tenant_id='00000000-0000-0000-0000-000000000099',
    entity_id='00000000-0000-0000-0000-000000000098',
    roles=['invoice_creator'],
))
")"

echo "=== API1: POST /invoices ==="
API1_RESPONSE="$(curl --fail-with-body --silent --show-error -X POST "$BASE_URL/api/v1/invoices" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000" \
  -d '{
    "customer_id": "00000000-0000-0000-0000-000000000005",
    "po_reference": "PO-2024-789",
    "invoice_date": "2026-07-19",
    "payment_terms": "NET30",
    "currency": "INR",
    "line_items": [
      {
        "description": "Industrial Pump",
        "quantity": 2,
        "unit_price": 50000,
        "tax_rate": 18,
        "tax_jurisdiction": "MH"
      },
      {
        "description": "Safety Valves",
        "quantity": 10,
        "unit_price": 5000,
        "tax_rate": 12,
        "tax_jurisdiction": "KA"
      }
    ]
  }')"
pretty_print "$API1_RESPONSE"
assert_json "$API1_RESPONSE" 'data["total_amount"] in {"174000.00", "174000.0000"}' "API1 server-calculated total is INR 174,000"
INVOICE_ID="$(JSON_RESPONSE="$API1_RESPONSE" python3 -c 'import json, os; print(json.loads(os.environ["JSON_RESPONSE"])["id"])')"

echo "=== API2: GET /invoices/{id} ==="
API2_RESPONSE="$(curl --fail-with-body --silent --show-error "$BASE_URL/api/v1/invoices/$INVOICE_ID" \
  -H "Authorization: Bearer $TOKEN")"
pretty_print "$API2_RESPONSE"
API2_ID="$(JSON_RESPONSE="$API2_RESPONSE" python3 -c 'import json, os; print(json.loads(os.environ["JSON_RESPONSE"])["id"])')"
test "$API2_ID" = "$INVOICE_ID"
assert_json "$API2_RESPONSE" 'len(data["line_items"]) == 2 and data["total_amount"] == "174000.0000"' "API2 returns the created invoice and line items"

echo "=== API3: POST /invoices/{id}/approve ==="
API3_RESPONSE="$(curl --fail-with-body --silent --show-error -X POST "$BASE_URL/api/v1/invoices/$INVOICE_ID/approve" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: 660e8400-e29b-41d4-a716-446655440001" \
  -H "If-Match: 1" \
  -d '{"notes": "Approved after tax jurisdiction check"}')"
pretty_print "$API3_RESPONSE"
assert_json "$API3_RESPONSE" 'data["status"] == "APPROVED" and data["version"] == 2' "API3 approves with optimistic version increment"

echo "=== API4: POST /payments ==="
API4_RESPONSE="$(curl --fail-with-body --silent --show-error -X POST "$BASE_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: 770e8400-e29b-41d4-a716-446655440002" \
  -d '{
    "customer_id": "00000000-0000-0000-0000-000000000005",
    "payment_reference": "PAY-2026-001",
    "payment_date": "2026-07-19",
    "amount": 100000,
    "currency": "INR",
    "payment_method": "NEFT",
    "allocation_mode": "AUTO"
  }')"
pretty_print "$API4_RESPONSE"
assert_json "$API4_RESPONSE" 'data["allocated_amount"] == "100000" and data["allocations"][0]["invoice_balance_after"] == "74000.0000"' "API4 allocates payment and leaves INR 74,000"

API4_RETRY="$(curl --fail-with-body --silent --show-error -X POST "$BASE_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: 770e8400-e29b-41d4-a716-446655440002" \
  -d '{
    "customer_id": "00000000-0000-0000-0000-000000000005",
    "payment_reference": "PAY-2026-001",
    "payment_date": "2026-07-19",
    "amount": 100000,
    "currency": "INR",
    "payment_method": "NEFT",
    "allocation_mode": "AUTO"
  }')"
PAYMENT_ID="$(JSON_RESPONSE="$API4_RESPONSE" python3 -c 'import json, os; print(json.loads(os.environ["JSON_RESPONSE"])["id"])')"
PAYMENT_RETRY_ID="$(JSON_RESPONSE="$API4_RETRY" python3 -c 'import json, os; print(json.loads(os.environ["JSON_RESPONSE"])["id"])')"
test "$PAYMENT_ID" = "$PAYMENT_RETRY_ID"
echo "PASS: API4 idempotent retry returns the original payment"

# The production design refreshes this MV every five minutes. Refresh explicitly
# in this deterministic integration test so API5 assertions do not depend on time.
docker compose exec -T db psql -U erp_user -d erp_db \
  -c "REFRESH MATERIALIZED VIEW ar_aging" >/dev/null

echo "=== API5: GET /customers/{id}/aging ==="
API5_RESPONSE="$(curl --fail-with-body --silent --show-error \
  "$BASE_URL/api/v1/customers/$CUSTOMER_ID/aging?entity_id=$ENTITY_ID" \
  -H "Authorization: Bearer $TOKEN")"
pretty_print "$API5_RESPONSE"
assert_json "$API5_RESPONSE" 'data["total_outstanding"] == "74000.0000" and data["buckets"]["current"]["invoice_count"] == 1' "API5 aging reconciles to INR 74,000"

echo "=== API6: GET /journal-entries?invoice={id} ==="
API6_RESPONSE="$(curl --fail-with-body --silent --show-error \
  "$BASE_URL/api/v1/journal-entries?invoice=$INVOICE_ID" \
  -H "Authorization: Bearer $TOKEN")"
pretty_print "$API6_RESPONSE"
assert_json "$API6_RESPONSE" 'data["pagination"]["total"] == 2 and all(entry["balanced"] for entry in data["journal_entries"]) and data["summary"]["net_ar_balance"] == "74000.0000"' "API6 returns balanced approval and payment journals"

echo "=== API7: GET /health ==="
API7_RESPONSE="$(curl --fail-with-body --silent --show-error "$BASE_URL/health")"
pretty_print "$API7_RESPONSE"
assert_json "$API7_RESPONSE" 'data["status"] == "healthy" and data["checks"]["reconciliation_status"] == "MATCHED"' "API7 reports healthy and reconciled"

echo "=== Control tests ==="
expect_status 404 "cross-tenant invoice access is concealed" \
  "$BASE_URL/api/v1/invoices/$INVOICE_ID" \
  -H "Authorization: Bearer $OTHER_TENANT_TOKEN"

expect_status 403 "invoice creator cannot call approval API" \
  -X POST "$BASE_URL/api/v1/invoices/$INVOICE_ID/approve" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Idempotency-Key: 880e8400-e29b-41d4-a716-446655440003" \
  -H "If-Match: 3" \
  -d '{"notes": "must be rejected"}'

expect_status 409 "idempotency key reuse with a different payment payload is rejected" \
  -X POST "$BASE_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: 770e8400-e29b-41d4-a716-446655440002" \
  -d '{
    "customer_id": "00000000-0000-0000-0000-000000000005",
    "payment_reference": "PAY-2026-001",
    "payment_date": "2026-07-19",
    "amount": 99999,
    "currency": "INR",
    "payment_method": "NEFT",
    "allocation_mode": "AUTO"
  }'

echo "=== All integration assertions passed ==="
