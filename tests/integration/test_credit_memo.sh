#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR"

BASE_URL="${BASE_URL:-http://localhost:8000}"
TENANT_ID="00000000-0000-0000-0000-000000000001"
ENTITY_ID="00000000-0000-0000-0000-000000000002"
OTHER_ENTITY_ID="00000000-0000-0000-0000-000000000096"
RAHUL_ID="00000000-0000-0000-0000-000000000003"
PRIYA_ID="00000000-0000-0000-0000-000000000004"
B6_CUSTOMER_ID="00000000-0000-0000-0000-000000000035"
FX_CUSTOMER_ID="00000000-0000-0000-0000-000000000032"

new_uuid() {
  python3 -c 'import uuid; print(uuid.uuid4())'
}

assert_json() {
  JSON_RESPONSE="$1" ASSERT_EXPR="$2" ASSERT_LABEL="$3" python3 -c '
import json
import os

data = json.loads(os.environ["JSON_RESPONSE"])
if not eval(os.environ["ASSERT_EXPR"], {"data": data, "float": float}):
    raise SystemExit("FAIL: " + os.environ["ASSERT_LABEL"])
print("PASS: " + os.environ["ASSERT_LABEL"])
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

db_scalar() {
  docker compose exec -T db psql -U erp_user -d erp_db -Atc "$1"
}

create_invoice() {
  local customer_id="$1"
  local currency="$2"
  local unit_price="$3"
  local tax_rate="$4"
  local response
  response="$(curl --fail-with-body --silent --show-error \
    -X POST "$BASE_URL/api/v1/invoices" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $RAHUL_TOKEN" \
    -H "X-Idempotency-Key: $(new_uuid)" \
    -d '{
      "customer_id": "'"$customer_id"'",
      "invoice_date": "2026-07-19",
      "payment_terms": "NET30",
      "currency": "'"$currency"'",
      "line_items": [{
        "description": "Credit memo control invoice",
        "quantity": 1,
        "unit_price": '"$unit_price"',
        "tax_rate": '"$tax_rate"'
      }]
    }')"
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$response"
}

approve_invoice() {
  curl --fail-with-body --silent --show-error \
    -X POST "$BASE_URL/api/v1/invoices/$1/approve" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $PRIYA_TOKEN" \
    -H "X-Idempotency-Key: $(new_uuid)" \
    -H "If-Match: 1" \
    -d '{"notes":"Approved for credit memo control test"}' >/dev/null
}

pay_invoice() {
  local invoice_id="$1"
  local customer_id="$2"
  local amount="$3"
  local currency="$4"
  local payment_date="$5"
  local unique_id
  unique_id="$(new_uuid)"
  curl --fail-with-body --silent --show-error \
    -X POST "$BASE_URL/api/v1/payments" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $PRIYA_TOKEN" \
    -H "X-Idempotency-Key: $unique_id" \
    -d '{
      "customer_id": "'"$customer_id"'",
      "payment_reference": "CM-PAY-'"$unique_id"'",
      "payment_date": "'"$payment_date"'",
      "amount": '"$amount"',
      "currency": "'"$currency"'",
      "payment_method": "NEFT",
      "allocation_mode": "MANUAL",
      "allocations": [{
        "invoice_id": "'"$invoice_id"'",
        "amount": '"$amount"'
      }]
    }' >/dev/null
}

assert_journal_balanced() {
  local journal_id="$1"
  local label="$2"
  local result
  result="$(db_scalar "
    SELECT
      (SUM(debit_amount) = SUM(credit_amount)) || '|' ||
      (SUM(base_debit_amount) = SUM(base_credit_amount))
    FROM journal_entry_line
    WHERE journal_entry_id = '$journal_id';
  ")"
  if [ "$result" != "true|true" ] && [ "$result" != "t|t" ]; then
    echo "FAIL: $label ($result)" >&2
    exit 1
  fi
  echo "PASS: $label"
}

echo "=== B6 setup ==="
docker compose exec -T app python -m src.seed_data >/dev/null
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

INSERT INTO customer (
  id, tenant_id, entity_id, name, currency, payment_terms, credit_limit
)
VALUES (
  '00000000-0000-0000-0000-000000000032',
  '00000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000002',
  'FX Test Customer', 'USD', 'NET30', 10000000
)
ON CONFLICT DO NOTHING;

INSERT INTO customer (
  id, tenant_id, entity_id, name, currency, payment_terms, credit_limit
)
VALUES (
  '00000000-0000-0000-0000-000000000035',
  '00000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000002',
  'Credit Memo Control Customer', 'INR', 'NET30', 10000000
)
ON CONFLICT DO NOTHING;
SQL

RAHUL_TOKEN="$(docker compose exec -T app python -c "
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

OTHER_ENTITY_TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token(
    user_id='$PRIYA_ID',
    tenant_id='$TENANT_ID',
    entity_id='$OTHER_ENTITY_ID',
    roles=['invoice_approver', 'cfo'],
))
")"

echo "=== B6 entity isolation and idempotency ==="
IDEM_INVOICE_ID="$(create_invoice "$B6_CUSTOMER_ID" INR 100 0)"
approve_invoice "$IDEM_INVOICE_ID"

expect_status 404 "credit memo conceals a sibling-entity invoice" \
  -X POST "$BASE_URL/api/v1/invoices/$IDEM_INVOICE_ID/credit-memos" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $OTHER_ENTITY_TOKEN" \
  -H "X-Idempotency-Key: $(new_uuid)" \
  -d '{"reason_code":"RETURN","description":"Wrong entity","amount":25,"include_tax":false}'

ISOLATION_COUNT="$(db_scalar "
  SELECT COUNT(*) FROM credit_memo WHERE invoice_id = '$IDEM_INVOICE_ID';
")"
if [ "$ISOLATION_COUNT" != "0" ]; then
  echo "FAIL: sibling-entity request created a credit memo" >&2
  exit 1
fi
echo "PASS: sibling-entity denial leaves no financial record"

IDEM_KEY="$(new_uuid)"
IDEM_PAYLOAD='{"reason_code":"RETURN","description":"Idempotency control","amount":25,"include_tax":false}'
IDEM_FIRST="$(curl --fail-with-body --silent --show-error \
  -X POST "$BASE_URL/api/v1/invoices/$IDEM_INVOICE_ID/credit-memos" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: $IDEM_KEY" \
  -d "$IDEM_PAYLOAD")"
IDEM_REPLAY="$(curl --fail-with-body --silent --show-error \
  -X POST "$BASE_URL/api/v1/invoices/$IDEM_INVOICE_ID/credit-memos" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: $IDEM_KEY" \
  -d "$IDEM_PAYLOAD")"
FIRST_CM_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$IDEM_FIRST")"
REPLAY_CM_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$IDEM_REPLAY")"
if [ "$FIRST_CM_ID" != "$REPLAY_CM_ID" ]; then
  echo "FAIL: idempotent replay returned a different credit memo" >&2
  exit 1
fi
echo "PASS: idempotent replay returns the original credit memo"

expect_status 409 "same idempotency key rejects a changed payload" \
  -X POST "$BASE_URL/api/v1/invoices/$IDEM_INVOICE_ID/credit-memos" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: $IDEM_KEY" \
  -d '{"reason_code":"RETURN","description":"Changed payload","amount":25,"include_tax":false}'

IDEM_COUNTS="$(db_scalar "
  SELECT
    (SELECT COUNT(*) FROM credit_memo WHERE invoice_id = '$IDEM_INVOICE_ID')
    || '|' ||
    (SELECT COUNT(*)
       FROM journal_entry je
       JOIN credit_memo cm ON cm.id = je.reference_id
      WHERE je.reference_type = 'CREDIT_MEMO'
        AND cm.invoice_id = '$IDEM_INVOICE_ID');
")"
if [ "$IDEM_COUNTS" != "1|1" ]; then
  echo "FAIL: replay created duplicate records ($IDEM_COUNTS)" >&2
  exit 1
fi
echo "PASS: replay creates one credit memo and one journal"

IDEM_JOURNALS="$(curl --fail-with-body --silent --show-error \
  "$BASE_URL/api/v1/journal-entries?invoice=$IDEM_INVOICE_ID" \
  -H "Authorization: Bearer $PRIYA_TOKEN")"
assert_json "$IDEM_JOURNALS" \
  'any(e["reference_type"] == "CREDIT_MEMO" and e["balanced"] for e in data["journal_entries"])' \
  "API6 exposes the balanced credit-memo journal"

echo "=== B6 concurrency ==="
RACE_INVOICE_ID="$(create_invoice "$B6_CUSTOMER_ID" INR 100 0)"
approve_invoice "$RACE_INVOICE_ID"
CM_TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$CM_TMP_DIR"' EXIT

curl --silent --show-error --output "$CM_TMP_DIR/a.json" --write-out "%{http_code}" \
  -X POST "$BASE_URL/api/v1/invoices/$RACE_INVOICE_ID/credit-memos" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: $(new_uuid)" \
  -d '{"reason_code":"RETURN","description":"Concurrent A","amount":80,"include_tax":false}' \
  > "$CM_TMP_DIR/a.status" &
CM_PID_A=$!
curl --silent --show-error --output "$CM_TMP_DIR/b.json" --write-out "%{http_code}" \
  -X POST "$BASE_URL/api/v1/invoices/$RACE_INVOICE_ID/credit-memos" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: $(new_uuid)" \
  -d '{"reason_code":"RETURN","description":"Concurrent B","amount":80,"include_tax":false}' \
  > "$CM_TMP_DIR/b.status" &
CM_PID_B=$!
wait "$CM_PID_A"
wait "$CM_PID_B"
RACE_STATUS_A="$(<"$CM_TMP_DIR/a.status")"
RACE_STATUS_B="$(<"$CM_TMP_DIR/b.status")"
RACE_STATUSES="$(printf '%s\n%s\n' "$RACE_STATUS_A" "$RACE_STATUS_B" | sort | tr '\n' ' ' | sed 's/ $//')"
if [ "$RACE_STATUSES" != "201 422" ]; then
  echo "FAIL: concurrent credits expected HTTP 201 and 422; got $RACE_STATUS_A and $RACE_STATUS_B" >&2
  exit 1
fi
echo "PASS: invoice row lock allows only one over-crediting race request"

RACE_RESULT="$(db_scalar "
  SELECT
    (SELECT COUNT(*) FROM credit_memo WHERE invoice_id = '$RACE_INVOICE_ID')
    || '|' ||
    (SELECT COUNT(*)
       FROM journal_entry je
       JOIN credit_memo cm ON cm.id = je.reference_id
      WHERE je.reference_type = 'CREDIT_MEMO'
        AND cm.invoice_id = '$RACE_INVOICE_ID')
    || '|' ||
    (SELECT balance_amount::text FROM invoice WHERE id = '$RACE_INVOICE_ID');
")"
if [ "$RACE_RESULT" != "1|1|20.0000" ]; then
  echo "FAIL: concurrent credit changed books incorrectly ($RACE_RESULT)" >&2
  exit 1
fi
echo "PASS: concurrency leaves one journal and INR 20 receivable"

echo "=== B6 paid and partially-paid invoices ==="
PARTIAL_INVOICE_ID="$(create_invoice "$B6_CUSTOMER_ID" INR 100 0)"
approve_invoice "$PARTIAL_INVOICE_ID"
pay_invoice "$PARTIAL_INVOICE_ID" "$B6_CUSTOMER_ID" 70 INR 2026-07-19
PARTIAL_CM="$(curl --fail-with-body --silent --show-error \
  -X POST "$BASE_URL/api/v1/invoices/$PARTIAL_INVOICE_ID/credit-memos" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: $(new_uuid)" \
  -d '{"reason_code":"RETURN","description":"Credit exceeds remaining AR","amount":50,"include_tax":false}')"
assert_json "$PARTIAL_CM" \
  'float(data["ar_reduction"]) == 30 and float(data["customer_credit_amount"]) == 20 and float(data["invoice_balance_after"]) == 0' \
  "partial-payment credit reduces AR only and records the excess liability"
PARTIAL_JOURNAL_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["journal_entry_id"])' <<< "$PARTIAL_CM")"
assert_journal_balanced "$PARTIAL_JOURNAL_ID" \
  "partial-payment credit journal balances in transaction and base currency"
PARTIAL_SPLIT="$(db_scalar "
  SELECT
    COALESCE(SUM(jel.credit_amount) FILTER (WHERE ga.account_code = '1200'), 0)
    || '|' ||
    COALESCE(SUM(jel.credit_amount) FILTER (WHERE ga.account_code = '2100'), 0)
  FROM journal_entry_line jel
  JOIN gl_account ga ON ga.id = jel.gl_account_id
  WHERE jel.journal_entry_id = '$PARTIAL_JOURNAL_ID';
")"
if [ "$PARTIAL_SPLIT" != "30.0000|20.0000" ]; then
  echo "FAIL: partial-payment credit used wrong AR/liability split ($PARTIAL_SPLIT)" >&2
  exit 1
fi
echo "PASS: partial-payment journal credits AR 30 and Customer Credit 20"

PAID_INVOICE_ID="$(create_invoice "$B6_CUSTOMER_ID" INR 100 0)"
approve_invoice "$PAID_INVOICE_ID"
pay_invoice "$PAID_INVOICE_ID" "$B6_CUSTOMER_ID" 100 INR 2026-07-19
PAID_CM="$(curl --fail-with-body --silent --show-error \
  -X POST "$BASE_URL/api/v1/invoices/$PAID_INVOICE_ID/credit-memos" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: $(new_uuid)" \
  -d '{"reason_code":"OVERCHARGE","description":"Post-payment price correction","amount":40,"include_tax":false}')"
assert_json "$PAID_CM" \
  'float(data["ar_reduction"]) == 0 and float(data["customer_credit_amount"]) == 40 and float(data["invoice_balance_after"]) == 0' \
  "paid-invoice credit creates a liability without over-crediting AR"
PAID_JOURNAL_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["journal_entry_id"])' <<< "$PAID_CM")"
assert_journal_balanced "$PAID_JOURNAL_ID" \
  "paid-invoice credit journal balances in transaction and base currency"

echo "=== B6 foreign-currency journal ==="
FX_CREATE="$(curl --fail-with-body --silent --show-error \
  -X POST "$BASE_URL/api/v1/invoices" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RAHUL_TOKEN" \
  -H "X-Idempotency-Key: $(new_uuid)" \
  -d '{
    "customer_id": "'"$FX_CUSTOMER_ID"'",
    "invoice_date": "2026-07-19",
    "payment_terms": "NET30",
    "currency": "USD",
    "line_items": [{
      "description": "USD credit memo control",
      "quantity": 1,
      "unit_price": 100,
      "tax_rate": 18
    }]
  }')"
FX_INVOICE_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$FX_CREATE")"
FX_RATE="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["exchange_rate"])' <<< "$FX_CREATE")"
approve_invoice "$FX_INVOICE_ID"
FX_CM="$(curl --fail-with-body --silent --show-error \
  -X POST "$BASE_URL/api/v1/invoices/$FX_INVOICE_ID/credit-memos" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: $(new_uuid)" \
  -d '{"reason_code":"RETURN","description":"Half returned","amount":59,"include_tax":true}')"
FX_RATE="$FX_RATE" FX_CM="$FX_CM" python3 -c '
import json
import os
from decimal import Decimal

data = json.loads(os.environ["FX_CM"])
expected_base = (Decimal("59") * Decimal(os.environ["FX_RATE"])).quantize(Decimal("0.0001"))
assert Decimal(data["subtotal_reversed"]) == Decimal("50")
assert Decimal(data["tax_reversed"]) == Decimal("9")
assert Decimal(data["base_amount"]) == expected_base
print("PASS: USD credit reverses USD 50 revenue and USD 9 tax at the locked invoice rate")
'
FX_JOURNAL_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["journal_entry_id"])' <<< "$FX_CM")"
assert_journal_balanced "$FX_JOURNAL_ID" \
  "USD credit journal balances in both USD and INR"

FX_JOURNALS="$(curl --fail-with-body --silent --show-error \
  "$BASE_URL/api/v1/journal-entries?invoice=$FX_INVOICE_ID" \
  -H "Authorization: Bearer $PRIYA_TOKEN")"
assert_json "$FX_JOURNALS" \
  'any(e["reference_type"] == "CREDIT_MEMO" and e["currency"] == "USD" and e["balanced"] for e in data["journal_entries"])' \
  "API6 returns the balanced USD credit-memo journal"

HEALTH_RESPONSE="$(curl --fail-with-body --silent --show-error "$BASE_URL/health")"
assert_json "$HEALTH_RESPONSE" \
  'data["status"] == "healthy" and data["checks"]["reconciliation_status"] == "MATCHED"' \
  "credit-memo controls preserve AR-to-GL reconciliation"

echo "=== All B6 credit-memo assertions passed ==="
