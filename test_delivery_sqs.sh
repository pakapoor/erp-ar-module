#!/usr/bin/env bash

set -Eeuo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
TENANT_ID="00000000-0000-0000-0000-000000000001"
ENTITY_ID="00000000-0000-0000-0000-000000000002"
OTHER_ENTITY_ID="00000000-0000-0000-0000-000000000096"
RAHUL_ID="00000000-0000-0000-0000-000000000003"
PRIYA_ID="00000000-0000-0000-0000-000000000004"
DELIVERY_CUSTOMER_ID="00000000-0000-0000-0000-000000000036"

cleanup() {
  docker compose start stub delivery_worker >/dev/null 2>&1 || true
}
trap cleanup EXIT

new_uuid() {
  python3 -c 'import uuid; print(uuid.uuid4())'
}

db_scalar() {
  docker compose exec -T db psql -U erp_user -d erp_db -Atc "$1"
}

expect_status() {
  local expected="$1"
  local label="$2"
  shift 2
  local actual
  actual="$(curl --silent --show-error --output /dev/null --write-out "%{http_code}" "$@")"
  if [ "$actual" != "$expected" ]; then
    echo "FAIL: $label (expected HTTP $expected, got $actual)" >&2
    exit 1
  fi
  echo "PASS: $label (HTTP $actual)"
}

wait_for_db_status() {
  local event_id="$1"
  local expected="$2"
  local attempt status
  for ((attempt = 1; attempt <= 45; attempt++)); do
    status="$(db_scalar "SELECT status FROM delivery_outbox WHERE id = '$event_id';")"
    if [ "$status" = "$expected" ]; then
      return 0
    fi
    sleep 1
  done
  echo "FAIL: event $event_id did not reach $expected (last status: $status)" >&2
  exit 1
}

create_invoice() {
  local response
  response="$(curl --fail-with-body --silent --show-error \
    -X POST "$BASE_URL/api/v1/invoices" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $RAHUL_TOKEN" \
    -H "X-Idempotency-Key: $(new_uuid)" \
    -d '{
      "customer_id": "'"$DELIVERY_CUSTOMER_ID"'",
      "invoice_date": "2026-07-19",
      "payment_terms": "NET30",
      "currency": "INR",
      "line_items": [{
        "description": "SQS delivery control invoice",
        "quantity": 1,
        "unit_price": 1000,
        "tax_rate": 18
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
    -d '{"notes":"Approved for SQS delivery control test"}' >/dev/null
}

echo "=== Delivery SQS setup ==="
docker compose exec -T app python -m src.seed_data >/dev/null
docker compose exec -T db psql -U erp_user -d erp_db -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
INSERT INTO customer (
  id, tenant_id, entity_id, name, email, currency, payment_terms, credit_limit
)
VALUES (
  '00000000-0000-0000-0000-000000000036',
  '00000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000002',
  'SQS Delivery Control Customer', 'delivery@example.com',
  'INR', 'NET30', 10000000
)
ON CONFLICT DO NOTHING;
SQL

RAHUL_TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token('$RAHUL_ID', '$TENANT_ID', '$ENTITY_ID', ['invoice_creator']))
")"
PRIYA_TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token('$PRIYA_ID', '$TENANT_ID', '$ENTITY_ID', ['invoice_approver', 'cfo']))
")"
OTHER_ENTITY_TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token('$PRIYA_ID', '$TENANT_ID', '$OTHER_ENTITY_ID', ['invoice_approver', 'cfo']))
")"

MAIN_QUEUE_URL="$(docker compose exec -T localstack awslocal sqs get-queue-url \
  --queue-name invoice-delivery --query QueueUrl --output text)"
DLQ_URL="$(docker compose exec -T localstack awslocal sqs get-queue-url \
  --queue-name invoice-delivery-dlq --query QueueUrl --output text)"
echo "PASS: LocalStack main queue and DLQ exist"

echo "=== Happy path: approval -> outbox -> SQS -> delivery ==="
HAPPY_INVOICE_ID="$(create_invoice)"
approve_invoice "$HAPPY_INVOICE_ID"
HAPPY_EVENT_ID="$(db_scalar "
  SELECT id FROM delivery_outbox
  WHERE invoice_id = '$HAPPY_INVOICE_ID' AND event_type = 'INVOICE_APPROVED';
")"
wait_for_db_status "$HAPPY_EVENT_ID" "DELIVERED"

HAPPY_STATE="$(db_scalar "
  SELECT status || '|' || (sqs_message_id IS NOT NULL) || '|' ||
         (published_at IS NOT NULL) || '|' || attempt_count
  FROM delivery_outbox WHERE id = '$HAPPY_EVENT_ID';
")"
if [ "$HAPPY_STATE" != "DELIVERED|true|true|1" ] && \
   [ "$HAPPY_STATE" != "DELIVERED|t|t|1" ]; then
  echo "FAIL: durable SQS lifecycle metadata is incomplete ($HAPPY_STATE)" >&2
  exit 1
fi
echo "PASS: approval commits a durable event that SQS delivers asynchronously"

DELIVERY_RESPONSE="$(curl --fail-with-body --silent --show-error \
  "$BASE_URL/api/v1/invoices/$HAPPY_INVOICE_ID/delivery" \
  -H "Authorization: Bearer $PRIYA_TOKEN")"
DELIVERY_RESPONSE="$DELIVERY_RESPONSE" python3 -c '
import json, os
data = json.loads(os.environ["DELIVERY_RESPONSE"])
event = data["events"][0]
assert event["status"] == "DELIVERED"
assert event["sqs_message_id"]
print("PASS: delivery status endpoint exposes the durable lifecycle")
'

expect_status 404 "delivery status conceals a sibling-entity invoice" \
  "$BASE_URL/api/v1/invoices/$HAPPY_INVOICE_ID/delivery" \
  -H "Authorization: Bearer $OTHER_ENTITY_TOKEN"

expect_status 409 "completed delivery cannot be retried" \
  -X POST "$BASE_URL/api/v1/delivery-events/$HAPPY_EVENT_ID/retry" \
  -H "Authorization: Bearer $PRIYA_TOKEN"

# Model the consumer crash window: the downstream accepted the event, but the
# broker redelivers it before the database acknowledgement. The stub must
# return its cached result for the same durable event ID.
DELIVERY_PAYLOAD="$(db_scalar "
  SELECT payload::text FROM delivery_outbox WHERE id = '$HAPPY_EVENT_ID';
")"
curl --fail-with-body --silent --show-error \
  -X POST http://localhost:9000/stub/send-invoice \
  -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: $HAPPY_EVENT_ID" \
  -d "$DELIVERY_PAYLOAD" >/dev/null
STUB_STATE="$(curl --fail-with-body --silent --show-error \
  "http://localhost:9000/stub/deliveries/$HAPPY_EVENT_ID")"
STUB_STATE="$STUB_STATE" python3 -c '
import json, os
data = json.loads(os.environ["STUB_STATE"])
assert data["accepted_count"] == 1
assert data["duplicate_count"] >= 1
print("PASS: downstream deduplicates at-least-once delivery by event ID")
'

echo "=== Failure path: SQS redrive -> DLQ -> CFO retry ==="
docker compose stop delivery_worker >/dev/null
DEAD_INVOICE_ID="$(create_invoice)"
approve_invoice "$DEAD_INVOICE_ID"
DEAD_EVENT_ID="$(db_scalar "
  SELECT id FROM delivery_outbox
  WHERE invoice_id = '$DEAD_INVOICE_ID' AND event_type = 'INVOICE_APPROVED';
")"
wait_for_db_status "$DEAD_EVENT_ID" "PUBLISHED"

docker compose stop stub >/dev/null
docker compose start delivery_worker >/dev/null
wait_for_db_status "$DEAD_EVENT_ID" "DEAD"

DLQ_MESSAGES=0
for ((attempt = 1; attempt <= 20; attempt++)); do
  DLQ_MESSAGES="$(docker compose exec -T localstack awslocal sqs get-queue-attributes \
    --queue-url "$DLQ_URL" \
    --attribute-names ApproximateNumberOfMessages \
    --query 'Attributes.ApproximateNumberOfMessages' --output text)"
  if [ "$DLQ_MESSAGES" -ge 1 ]; then
    break
  fi
  sleep 1
done
if [ "$DLQ_MESSAGES" -lt 1 ]; then
  echo "FAIL: failed delivery did not redrive to the DLQ" >&2
  exit 1
fi
echo "PASS: three failed receives move the message to the DLQ"

# Financial truth remains committed even while the notification is dead.
DEAD_FINANCIAL_STATE="$(db_scalar "
  SELECT i.status || '|' || COUNT(je.id)
  FROM invoice i
  LEFT JOIN journal_entry je
    ON je.reference_type = 'INVOICE' AND je.reference_id = i.id
  WHERE i.id = '$DEAD_INVOICE_ID'
  GROUP BY i.status;
")"
if [ "$DEAD_FINANCIAL_STATE" != "APPROVED|1" ]; then
  echo "FAIL: delivery failure changed financial truth ($DEAD_FINANCIAL_STATE)" >&2
  exit 1
fi
echo "PASS: delivery failure does not roll back approval or its GL entry"

docker compose start stub >/dev/null
for ((attempt = 1; attempt <= 30; attempt++)); do
  if curl --fail --silent --output /dev/null http://localhost:9000/health; then
    break
  fi
  sleep 1
done

RETRY_RESPONSE="$(curl --fail-with-body --silent --show-error \
  -X POST "$BASE_URL/api/v1/delivery-events/$DEAD_EVENT_ID/retry" \
  -H "Authorization: Bearer $PRIYA_TOKEN")"
RETRY_RESPONSE="$RETRY_RESPONSE" python3 -c '
import json, os
data = json.loads(os.environ["RETRY_RESPONSE"])
assert data["status"] == "PENDING"
print("PASS: CFO can requeue a dead event without touching financial records")
'
wait_for_db_status "$DEAD_EVENT_ID" "DELIVERED"

RETRIED_INVOICE_STATE="$(db_scalar "
  SELECT status || '|' || version FROM invoice WHERE id = '$DEAD_INVOICE_ID';
")"
if [ "$RETRIED_INVOICE_STATE" != "SENT|3" ]; then
  echo "FAIL: retried delivery did not mark invoice SENT exactly once ($RETRIED_INVOICE_STATE)" >&2
  exit 1
fi
echo "PASS: retried delivery succeeds and marks the invoice SENT"

echo "=== Delivery SQS integration tests passed ==="
