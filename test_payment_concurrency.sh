#!/usr/bin/env bash

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
TENANT_ID="00000000-0000-0000-0000-000000000001"
ENTITY_ID="00000000-0000-0000-0000-000000000002"
RAHUL_ID="00000000-0000-0000-0000-000000000003"
PRIYA_ID="00000000-0000-0000-0000-000000000004"
CUSTOMER_ID="00000000-0000-0000-0000-000000000036"

new_uuid() {
  python3 -c 'import uuid; print(uuid.uuid4())'
}

docker compose exec -T app python -m src.seed_data >/dev/null
docker compose exec -T db psql -U erp_user -d erp_db -v ON_ERROR_STOP=1 \
  -c "INSERT INTO customer (
        id, tenant_id, entity_id, name, currency, payment_terms, credit_limit
      ) VALUES (
        '$CUSTOMER_ID', '$TENANT_ID', '$ENTITY_ID',
        'Payment Concurrency Control', 'INR', 'NET30', 10000000
      ) ON CONFLICT DO NOTHING;" >/dev/null

TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token(
    user_id='$RAHUL_ID', tenant_id='$TENANT_ID', entity_id='$ENTITY_ID',
    roles=['invoice_creator'],
))
")"

PRIYA_TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token(
    user_id='$PRIYA_ID', tenant_id='$TENANT_ID', entity_id='$ENTITY_ID',
    roles=['invoice_approver', 'cfo'],
))
")"

RACE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/erp-ar-payment-race.XXXXXX")"
cleanup() {
  rm -f "$RACE_DIR"/body-*.json "$RACE_DIR"/status-*
  rmdir "$RACE_DIR"
}
trap cleanup EXIT

run_payment_race() {
  local allocation_mode=$1
  local run_id create_key approve_key payment_key_1 payment_key_2
  local create_response invoice_id allocation_fragment payload_1 payload_2
  local pid_1 pid_2 status_1 status_2 sorted_statuses conflict_body db_state
  local pre_payment_state pre_payment_version expected_db_state

  run_id="$(new_uuid)"
  create_key="$(new_uuid)"
  approve_key="$(new_uuid)"
  payment_key_1="$(new_uuid)"
  payment_key_2="$(new_uuid)"

  echo "=== NFR1 $allocation_mode: create and approve control invoice ==="
  create_response="$(curl --fail-with-body --silent --show-error \
    -X POST "$BASE_URL/api/v1/invoices" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Idempotency-Key: $create_key" \
    -d "{
      \"customer_id\": \"$CUSTOMER_ID\",
      \"po_reference\": \"$allocation_mode-RACE-$run_id\",
      \"invoice_date\": \"2026-07-19\",
      \"payment_terms\": \"NET30\",
      \"currency\": \"INR\",
      \"line_items\": [{
        \"description\": \"$allocation_mode payment concurrency control\",
        \"quantity\": 1,
        \"unit_price\": 100,
        \"tax_rate\": 0
      }]
    }")"
  invoice_id="$(JSON_RESPONSE="$create_response" python3 -c \
    'import json, os; print(json.loads(os.environ["JSON_RESPONSE"])["id"])')"

  curl --fail-with-body --silent --show-error \
    -X POST "$BASE_URL/api/v1/invoices/$invoice_id/approve" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $PRIYA_TOKEN" \
    -H "X-Idempotency-Key: $approve_key" \
    -H "If-Match: 1" \
    -d '{"notes":"Payment concurrency control"}' >/dev/null

  # The delivery worker legitimately advances APPROVED -> SENT and increments
  # the version. Let that independent transition settle so this test can prove
  # that the winning payment contributes exactly one further version change.
  pre_payment_state=""
  for _ in $(seq 1 40); do
    pre_payment_state="$(docker compose exec -T db psql -U erp_user -d erp_db -AtF '|' -c "
      SELECT status, version FROM invoice WHERE id = '$invoice_id';
    ")"
    if [[ "$pre_payment_state" == SENT\|* ]]; then
      break
    fi
    sleep 0.25
  done
  if [[ "$pre_payment_state" != SENT\|* ]]; then
    echo "FAIL: $allocation_mode delivery did not settle before race: $pre_payment_state" >&2
    exit 1
  fi
  pre_payment_version="${pre_payment_state#SENT|}"
  expected_db_state="PAID|0.0000|$((pre_payment_version + 1))|1|1"

  allocation_fragment=""
  if [ "$allocation_mode" = "MANUAL" ]; then
    allocation_fragment=", \"allocations\": [{\"invoice_id\": \"$invoice_id\", \"amount\": 100}]"
  fi
  payload_1="{
    \"customer_id\": \"$CUSTOMER_ID\",
    \"payment_reference\": \"$allocation_mode-RACE-A-$run_id\",
    \"payment_date\": \"2026-07-19\",
    \"amount\": 100,
    \"currency\": \"INR\",
    \"payment_method\": \"NEFT\",
    \"allocation_mode\": \"$allocation_mode\"$allocation_fragment
  }"
  payload_2="${payload_1/RACE-A-/RACE-B-}"

  echo "=== NFR1 $allocation_mode: race two distinct full payments ==="
  curl --silent --show-error -X POST "$BASE_URL/api/v1/payments" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $PRIYA_TOKEN" \
    -H "X-Idempotency-Key: $payment_key_1" \
    -d "$payload_1" -o "$RACE_DIR/body-$allocation_mode-1.json" \
    -w '%{http_code}' >"$RACE_DIR/status-$allocation_mode-1" &
  pid_1=$!

  curl --silent --show-error -X POST "$BASE_URL/api/v1/payments" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $PRIYA_TOKEN" \
    -H "X-Idempotency-Key: $payment_key_2" \
    -d "$payload_2" -o "$RACE_DIR/body-$allocation_mode-2.json" \
    -w '%{http_code}' >"$RACE_DIR/status-$allocation_mode-2" &
  pid_2=$!

  wait "$pid_1"
  wait "$pid_2"

  status_1="$(<"$RACE_DIR/status-$allocation_mode-1")"
  status_2="$(<"$RACE_DIR/status-$allocation_mode-2")"
  sorted_statuses="$(printf '%s\n%s\n' "$status_1" "$status_2" | sort | tr '\n' ' ')"
  if [ "$sorted_statuses" != "201 409 " ]; then
    echo "FAIL: $allocation_mode expected HTTP 201/409; got $status_1/$status_2" >&2
    python3 -m json.tool "$RACE_DIR/body-$allocation_mode-1.json" >&2 || true
    python3 -m json.tool "$RACE_DIR/body-$allocation_mode-2.json" >&2 || true
    exit 1
  fi

  conflict_body="$RACE_DIR/body-$allocation_mode-1.json"
  if [ "$status_2" = "409" ]; then
    conflict_body="$RACE_DIR/body-$allocation_mode-2.json"
  fi
  python3 -c '
import json, sys
body = json.load(open(sys.argv[1]))
assert body["error"]["code"] == "PAYMENT_CONCURRENCY_CONFLICT", body
' "$conflict_body"

  db_state="$(docker compose exec -T db psql -U erp_user -d erp_db -AtF '|' -c "
  SELECT
    i.status, i.balance_amount, i.version,
    COUNT(DISTINCT pa.payment_id), COUNT(DISTINCT je.id)
  FROM invoice i
  LEFT JOIN payment_allocation pa ON pa.invoice_id = i.id
  LEFT JOIN journal_entry je
    ON je.reference_type = 'PAYMENT' AND je.reference_id = pa.payment_id
  WHERE i.id = '$invoice_id'
  GROUP BY i.id;
  ")"
  if [ "$db_state" != "$expected_db_state" ]; then
    echo "FAIL: $allocation_mode unexpected committed state: $db_state" >&2
    echo "Expected one payment increment from version $pre_payment_version: $expected_db_state" >&2
    exit 1
  fi
  echo "PASS: $allocation_mode has one 201, one retryable 409, and one posting"
}

run_payment_race MANUAL
run_payment_race AUTO

HEALTH_STATUS="$(curl --silent --show-error --output /dev/null \
  --write-out '%{http_code}' "$BASE_URL/health")"
test "$HEALTH_STATUS" = "200"
echo "PASS: both races leave AR/GL reconciliation healthy"
