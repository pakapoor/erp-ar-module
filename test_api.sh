#!/bin/bash

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
TENANT_ID="00000000-0000-0000-0000-000000000001"
ENTITY_ID="00000000-0000-0000-0000-000000000002"
OTHER_ENTITY_ID="00000000-0000-0000-0000-000000000096"
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

new_uuid() {
  python3 -c 'import uuid; print(uuid.uuid4())'
}

echo "=== Setup: seed data and runtime JWTs ==="
docker compose exec -T app python -m src.seed_data >/dev/null

echo "=== FX feed contract: deterministic ECB parser/derivation ==="
docker compose exec -T app python -m unittest -q \
  src.tests.test_fx_rate_worker src.tests.test_invoice_fx
echo "PASS: ECB validation, INR cross-rate derivation and invoice FX rounding"

# Deterministic fallback for the API1 integration test. A live ECB row dated
# 2026-07-17 wins when present; otherwise this three-day-old fixture is valid.
docker compose exec -T db psql -U erp_user -d erp_db -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
INSERT INTO exchange_rate (
  id, tenant_id, from_currency, to_currency, rate, effective_date,
  source, rate_type, status, provider_effective_date, fetched_at,
  is_derived, is_manual_override, approved_at
)
VALUES (
  '00000000-0000-0000-0000-000000000031',
  '00000000-0000-0000-0000-000000000001',
  'USD', 'INR', 96.00000000, '2026-07-16',
  'TEST_FIXTURE', 'DAILY_REFERENCE', 'APPROVED', '2026-07-16',
  CURRENT_TIMESTAMP, FALSE, FALSE, CURRENT_TIMESTAMP
)
ON CONFLICT DO NOTHING;
SQL

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

INVALID_TOKEN="$(docker compose exec -T app python -c "
import time
from jose import jwt
print(jwt.encode(
    {
        'sub': '$RAHUL_ID',
        'user_id': '$RAHUL_ID',
        'tenant_id': '$TENANT_ID',
        'entity_id': '$ENTITY_ID',
        'roles': ['invoice_creator'],
        'exp': int(time.time()) + 3600,
        'iat': int(time.time()),
    },
    'wrong-dev-secret',
    algorithm='HS256',
    headers={'kid': 'dev-key-001'},
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

OTHER_ENTITY_TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token(
    user_id='$RAHUL_ID',
    tenant_id='$TENANT_ID',
    entity_id='$OTHER_ENTITY_ID',
    roles=['invoice_creator'],
))
")"

echo "=== L7 Gateway controls ==="
expect_status 401 "gateway rejects a protected request with no JWT" \
  "$BASE_URL/api/v1/invoices/00000000-0000-0000-0000-000000000000"

expect_status 401 "gateway rejects a JWT with an invalid signature" \
  "$BASE_URL/api/v1/invoices/00000000-0000-0000-0000-000000000000" \
  -H "Authorization: Bearer $INVALID_TOKEN"

APP_PUBLISHED_PORT="$(
  docker inspect erp_app \
    --format '{{json (index .NetworkSettings.Ports "8080/tcp")}}'
)"
if [ "$APP_PUBLISHED_PORT" != "null" ]; then
  echo "FAIL: internal AR application exposes a host port" >&2
  exit 1
fi
echo "PASS: AR application has no host-published port"

docker compose exec -T -e INVALID_TOKEN="$INVALID_TOKEN" app python -c '
import os
import httpx

response = httpx.get(
    "http://localhost:8080/api/v1/invoices/00000000-0000-0000-0000-000000000000",
    headers={"Authorization": "Bearer " + os.environ["INVALID_TOKEN"]},
)
if response.status_code != 401:
    raise SystemExit(
        f"FAIL: app Zero Trust validation expected HTTP 401, got {response.status_code}"
    )
print("PASS: internal AR application independently rejects the invalid JWT")
'

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

echo "=== API1 FX: POST /invoices in USD ==="
FX_API1_RESPONSE="$(curl --fail-with-body --silent --show-error -X POST "$BASE_URL/api/v1/invoices" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Idempotency-Key: 880e8400-e29b-41d4-a716-446655440004" \
  -d '{
    "customer_id": "00000000-0000-0000-0000-000000000005",
    "po_reference": "FX-API1-TEST",
    "invoice_date": "2026-07-19",
    "payment_terms": "NET30",
    "currency": "usd",
    "line_items": [
      {
        "description": "USD equipment",
        "quantity": 1,
        "unit_price": 1000,
        "tax_rate": 18,
        "tax_jurisdiction": "MH"
      }
    ]
  }')"
pretty_print "$FX_API1_RESPONSE"
assert_json "$FX_API1_RESPONSE" 'data["currency"] == "USD" and data["base_currency"] == "INR" and data["exchange_rate_id"] is not None' "API1 locks an approved USD/INR rate"
assert_json "$FX_API1_RESPONSE" 'abs(float(data["base_total_amount"]) - float(data["base_subtotal_amount"]) - float(data["base_tax_amount"])) < 0.00001' "API1 base components produce a balanced base total"

expect_status 503 "API1 rejects a stale foreign-exchange rate" \
  -X POST "$BASE_URL/api/v1/invoices" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Idempotency-Key: 880e8400-e29b-41d4-a716-446655440005" \
  -d '{
    "customer_id": "00000000-0000-0000-0000-000000000005",
    "po_reference": "FX-STALE-TEST",
    "invoice_date": "2026-07-01",
    "payment_terms": "NET30",
    "currency": "USD",
    "line_items": [{"description": "Stale FX test", "quantity": 1, "unit_price": 1}]
  }'

STALE_INVOICE_COUNT="$(docker compose exec -T db psql -U erp_user -d erp_db -Atc \
  "SELECT COUNT(*) FROM invoice WHERE po_reference = 'FX-STALE-TEST';")"
test "$STALE_INVOICE_COUNT" = "0"
echo "PASS: stale-rate rejection rolls back the complete invoice transaction"

echo "=== API2: GET /invoices/{id} ==="
API2_RESPONSE="$(curl --fail-with-body --silent --show-error "$BASE_URL/api/v1/invoices/$INVOICE_ID" \
  -H "Authorization: Bearer $TOKEN")"
pretty_print "$API2_RESPONSE"
API2_ID="$(JSON_RESPONSE="$API2_RESPONSE" python3 -c 'import json, os; print(json.loads(os.environ["JSON_RESPONSE"])["id"])')"
test "$API2_ID" = "$INVOICE_ID"
assert_json "$API2_RESPONSE" 'len(data["line_items"]) == 2 and data["total_amount"] == "174000.0000"' "API2 returns the created invoice and line items"

TRACE_ID="$(curl --fail-with-body --silent --show-error --dump-header - \
  --output /dev/null "$BASE_URL/api/v1/invoices/$INVOICE_ID" \
  -H "Authorization: Bearer $TOKEN" \
  | awk 'tolower($1) == "x-trace-id:" {print $2}' \
  | tr -d '\r')"
if [ -z "$TRACE_ID" ]; then
  echo "FAIL: gateway/application response did not contain X-Trace-ID" >&2
  exit 1
fi
echo "PASS: gateway request ID propagates as X-Trace-ID ($TRACE_ID)"

echo "=== API3: POST /invoices/{id}/approve ==="
API3_RESPONSE="$(curl --fail-with-body --silent --show-error -X POST "$BASE_URL/api/v1/invoices/$INVOICE_ID/approve" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: 660e8400-e29b-41d4-a716-446655440001" \
  -H "If-Match: 1" \
  -d '{"notes": "Approved after tax jurisdiction check"}')"
pretty_print "$API3_RESPONSE"
assert_json "$API3_RESPONSE" 'data["status"] == "APPROVED" and data["version"] == 2' "API3 approves with optimistic version increment"

# Approval and its PENDING outbox row commit atomically. The separate worker
# should deliver the event and update its durable state shortly afterward.
DELIVERY_STATUS=""
for _ in 1 2 3 4 5; do
  DELIVERY_STATUS="$(docker compose exec -T db psql -U erp_user -d erp_db -Atc \
    "SELECT status FROM delivery_outbox WHERE invoice_id='$INVOICE_ID' AND event_type='INVOICE_APPROVED';")"
  [ "$DELIVERY_STATUS" = "DELIVERED" ] && break
  sleep 2
done
if [ "$DELIVERY_STATUS" != "DELIVERED" ]; then
  echo "FAIL: invoice delivery outbox was not delivered (status=$DELIVERY_STATUS)" >&2
  exit 1
fi
echo "PASS: approval outbox event was delivered by the separate worker"

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

echo "=== API6 pagination: page_size=1 ==="
API6_PAGE1="$(curl --fail-with-body --silent --show-error \
  "$BASE_URL/api/v1/journal-entries?invoice=$INVOICE_ID&page=1&page_size=1" \
  -H "Authorization: Bearer $TOKEN")"
API6_PAGE2="$(curl --fail-with-body --silent --show-error \
  "$BASE_URL/api/v1/journal-entries?invoice=$INVOICE_ID&page=2&page_size=1" \
  -H "Authorization: Bearer $TOKEN")"
API6_PAGE3="$(curl --fail-with-body --silent --show-error \
  "$BASE_URL/api/v1/journal-entries?invoice=$INVOICE_ID&page=3&page_size=1" \
  -H "Authorization: Bearer $TOKEN")"

assert_json "$API6_PAGE1" \
  'data["pagination"] == {"page": 1, "page_size": 1, "total": 2, "total_pages": 2} and len(data["journal_entries"]) == 1 and data["summary"]["net_ar_balance"] == "74000.0000"' \
  "API6 page 1 returns one entry and invoice-wide summary"
assert_json "$API6_PAGE2" \
  'data["pagination"] == {"page": 2, "page_size": 1, "total": 2, "total_pages": 2} and len(data["journal_entries"]) == 1 and data["summary"]["net_ar_balance"] == "74000.0000"' \
  "API6 page 2 returns one entry and the same invoice-wide summary"
assert_json "$API6_PAGE3" \
  'data["pagination"] == {"page": 3, "page_size": 1, "total": 2, "total_pages": 2} and data["journal_entries"] == [] and data["summary"]["net_ar_balance"] == "74000.0000"' \
  "API6 out-of-range page returns HTTP 200 with an empty list"

API6_PAGE1_ID="$(JSON_RESPONSE="$API6_PAGE1" python3 -c \
  'import json, os; print(json.loads(os.environ["JSON_RESPONSE"])["journal_entries"][0]["id"])')"
API6_PAGE2_ID="$(JSON_RESPONSE="$API6_PAGE2" python3 -c \
  'import json, os; print(json.loads(os.environ["JSON_RESPONSE"])["journal_entries"][0]["id"])')"
if [ "$API6_PAGE1_ID" = "$API6_PAGE2_ID" ]; then
  echo "FAIL: API6 pagination repeated the same journal entry" >&2
  exit 1
fi
echo "PASS: API6 page ordering is deterministic without duplicate entries"

expect_status 422 "API6 rejects page zero" \
  "$BASE_URL/api/v1/journal-entries?invoice=$INVOICE_ID&page=0&page_size=1" \
  -H "Authorization: Bearer $TOKEN"

expect_status 422 "API6 enforces maximum page size 100" \
  "$BASE_URL/api/v1/journal-entries?invoice=$INVOICE_ID&page=1&page_size=101" \
  -H "Authorization: Bearer $TOKEN"

echo "=== API7: GET /health ==="
API7_RESPONSE="$(curl --fail-with-body --silent --show-error "$BASE_URL/health")"
pretty_print "$API7_RESPONSE"
assert_json "$API7_RESPONSE" 'data["status"] == "healthy" and data["checks"]["reconciliation_status"] == "MATCHED"' "API7 reports healthy and reconciled"

echo "=== Control tests ==="
expect_status 404 "cross-tenant invoice access is concealed" \
  "$BASE_URL/api/v1/invoices/$INVOICE_ID" \
  -H "Authorization: Bearer $OTHER_TENANT_TOKEN"

expect_status 404 "cross-entity invoice access is concealed" \
  "$BASE_URL/api/v1/invoices/$INVOICE_ID" \
  -H "Authorization: Bearer $OTHER_ENTITY_TOKEN"

expect_status 404 "cross-entity journal access is concealed" \
  "$BASE_URL/api/v1/journal-entries?invoice=$INVOICE_ID" \
  -H "Authorization: Bearer $OTHER_ENTITY_TOKEN"

expect_status 404 "cross-entity aging access is concealed" \
  "$BASE_URL/api/v1/customers/$CUSTOMER_ID/aging?entity_id=$ENTITY_ID" \
  -H "Authorization: Bearer $OTHER_ENTITY_TOKEN"

# Reusing entity A's idempotency key from entity B must not return entity A's
# cached invoice. Entity B cannot access entity A's customer, so this is 404.
expect_status 404 "idempotency cache is scoped by entity" \
  -X POST "$BASE_URL/api/v1/invoices" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $OTHER_ENTITY_TOKEN" \
  -H "X-Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000" \
  -d '{
    "customer_id": "00000000-0000-0000-0000-000000000005",
    "po_reference": "PO-2024-789",
    "invoice_date": "2026-07-19",
    "payment_terms": "NET30",
    "currency": "INR",
    "line_items": [{
      "description": "Industrial Pump",
      "quantity": 2,
      "unit_price": 50000,
      "tax_rate": 18,
      "tax_jurisdiction": "MH"
    }, {
      "description": "Safety Valves",
      "quantity": 10,
      "unit_price": 5000,
      "tax_rate": 12,
      "tax_jurisdiction": "KA"
    }]
  }'

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

echo "=== FR4 control: overpayment liability journal ==="
OVERPAY_INVOICE_RESPONSE="$(curl --fail-with-body --silent --show-error \
  -X POST "$BASE_URL/api/v1/invoices" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Idempotency-Key: 990e8400-e29b-41d4-a716-446655440010" \
  -d '{
    "customer_id": "00000000-0000-0000-0000-000000000005",
    "po_reference": "PO-OVERPAYMENT-001",
    "invoice_date": "2026-07-19",
    "payment_terms": "NET30",
    "currency": "INR",
    "line_items": [{
      "description": "Overpayment test item",
      "quantity": 1,
      "unit_price": 1000,
      "tax_rate": 0,
      "tax_jurisdiction": "MH"
    }]
  }')"
OVERPAY_INVOICE_ID="$(JSON_RESPONSE="$OVERPAY_INVOICE_RESPONSE" python3 -c \
  'import json, os; print(json.loads(os.environ["JSON_RESPONSE"])["id"])')"

curl --fail-with-body --silent --show-error -X POST \
  "$BASE_URL/api/v1/invoices/$OVERPAY_INVOICE_ID/approve" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: 990e8400-e29b-41d4-a716-446655440011" \
  -H "If-Match: 1" \
  -d '{"notes": "Approve overpayment control invoice"}' >/dev/null

OVERPAY_RESPONSE="$(curl --fail-with-body --silent --show-error -X POST \
  "$BASE_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: 990e8400-e29b-41d4-a716-446655440012" \
  -d '{
    "customer_id": "00000000-0000-0000-0000-000000000005",
    "payment_reference": "PAY-OVERPAYMENT-001",
    "payment_date": "2026-07-19",
    "amount": 1500,
    "currency": "INR",
    "payment_method": "NEFT",
    "allocation_mode": "MANUAL",
    "allocations": [{
      "invoice_id": "'"$OVERPAY_INVOICE_ID"'",
      "amount": 1000
    }]
  }')"
pretty_print "$OVERPAY_RESPONSE"
assert_json "$OVERPAY_RESPONSE" \
  'data["allocated_amount"] == "1000" and data["overpayment_amount"] == "500" and data["overpayment_action"] == "ON_ACCOUNT"' \
  "FR4 records INR 500 unapplied customer credit"

OVERPAY_JOURNALS="$(curl --fail-with-body --silent --show-error \
  "$BASE_URL/api/v1/journal-entries?invoice=$OVERPAY_INVOICE_ID" \
  -H "Authorization: Bearer $TOKEN")"
assert_json "$OVERPAY_JOURNALS" \
  'all(entry["balanced"] for entry in data["journal_entries"]) and any(line["account_code"] == "2100" and line["credit_amount"] == "500.0000" for entry in data["journal_entries"] for line in entry["lines"])' \
  "FR4 overpayment journal balances with credit to 2100 Customer Credit"

echo "=== T6 controls: stale and concurrent approvals ==="
T6_CREATE_KEY="$(new_uuid)"
T6_STALE_KEY="$(new_uuid)"
T6_APPROVE_KEY_A="$(new_uuid)"
T6_APPROVE_KEY_B="$(new_uuid)"
T6_PAYMENT_KEY="$(new_uuid)"
T6_PO_REFERENCE="T6-$(new_uuid)"
T6_PAYMENT_REFERENCE="T6-PAY-$(new_uuid)"
T6_INVOICE_RESPONSE="$(curl --fail-with-body --silent --show-error \
  -X POST "$BASE_URL/api/v1/invoices" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Idempotency-Key: $T6_CREATE_KEY" \
  -d '{
    "customer_id": "00000000-0000-0000-0000-000000000005",
    "po_reference": "'"$T6_PO_REFERENCE"'",
    "invoice_date": "2026-07-19",
    "payment_terms": "NET30",
    "currency": "INR",
    "line_items": [{
      "description": "Concurrency control item",
      "quantity": 1,
      "unit_price": 100,
      "tax_rate": 0,
      "tax_jurisdiction": "MH"
    }]
  }')"
T6_INVOICE_ID="$(JSON_RESPONSE="$T6_INVOICE_RESPONSE" python3 -c \
  'import json, os; print(json.loads(os.environ["JSON_RESPONSE"])["id"])')"

expect_status 409 "stale If-Match version is rejected" \
  -X POST "$BASE_URL/api/v1/invoices/$T6_INVOICE_ID/approve" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: $T6_STALE_KEY" \
  -H "If-Match: 0" \
  -d '{"notes": "stale version must fail"}'

T6_TMP_DIR="$(mktemp -d)"
curl --silent --show-error --output "$T6_TMP_DIR/a.json" --write-out "%{http_code}" \
  -X POST "$BASE_URL/api/v1/invoices/$T6_INVOICE_ID/approve" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: $T6_APPROVE_KEY_A" \
  -H "If-Match: 1" \
  -d '{"notes": "concurrent approval A"}' > "$T6_TMP_DIR/a.status" &
T6_PID_A=$!
curl --silent --show-error --output "$T6_TMP_DIR/b.json" --write-out "%{http_code}" \
  -X POST "$BASE_URL/api/v1/invoices/$T6_INVOICE_ID/approve" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: $T6_APPROVE_KEY_B" \
  -H "If-Match: 1" \
  -d '{"notes": "concurrent approval B"}' > "$T6_TMP_DIR/b.status" &
T6_PID_B=$!
wait "$T6_PID_A"
wait "$T6_PID_B"
T6_STATUS_A="$(<"$T6_TMP_DIR/a.status")"
T6_STATUS_B="$(<"$T6_TMP_DIR/b.status")"
rm -rf "$T6_TMP_DIR"

T6_SORTED_STATUSES="$(printf '%s\n%s\n' "$T6_STATUS_A" "$T6_STATUS_B" | sort | tr '\n' ' ' | sed 's/ $//')"
if [ "$T6_SORTED_STATUSES" != "200 409" ]; then
  echo "FAIL: concurrent approvals expected HTTP 200 and 409; got $T6_STATUS_A and $T6_STATUS_B" >&2
  exit 1
fi
echo "PASS: exactly one concurrent approval succeeds; the loser receives HTTP 409"

T6_RECORD_COUNTS="$(docker compose exec -T db psql -U erp_user -d erp_db -Atc \
  "SELECT
     (SELECT COUNT(*) FROM journal_entry WHERE reference_type='INVOICE' AND reference_id='$T6_INVOICE_ID')
     || '|' ||
     (SELECT COUNT(*) FROM delivery_outbox WHERE invoice_id='$T6_INVOICE_ID' AND event_type='INVOICE_APPROVED');")"
if [ "$T6_RECORD_COUNTS" != "1|1" ]; then
  echo "FAIL: concurrent approval created duplicate financial/delivery records ($T6_RECORD_COUNTS)" >&2
  exit 1
fi
echo "PASS: concurrent approval creates exactly one GL entry and one outbox event"

# Bring the test invoice back to zero so repeated suite runs do not change the
# deterministic Tata Steel aging assertion on the next MV refresh.
T6_PAYMENT_RESPONSE="$(curl --fail-with-body --silent --show-error -X POST \
  "$BASE_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: $T6_PAYMENT_KEY" \
  -d '{
    "customer_id": "00000000-0000-0000-0000-000000000005",
    "payment_reference": "'"$T6_PAYMENT_REFERENCE"'",
    "payment_date": "2026-07-19",
    "amount": 100,
    "currency": "INR",
    "payment_method": "NEFT",
    "allocation_mode": "MANUAL",
    "allocations": [{
      "invoice_id": "'"$T6_INVOICE_ID"'",
      "amount": 100
    }]
  }')"
assert_json "$T6_PAYMENT_RESPONSE" \
  'data["allocations"][0]["invoice_status"] == "PAID" and data["unallocated_amount"] == "0"' \
  "T6 control invoice is settled so the suite remains repeatable"

echo "=== All integration assertions passed ==="
