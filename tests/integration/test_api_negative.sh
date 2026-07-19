#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR"

BASE_URL="${BASE_URL:-http://localhost:8000}"

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

TOKEN="$(docker compose exec -T app python -c '
from src.auth import create_test_token
print(create_test_token(
    user_id="00000000-0000-0000-0000-000000000003",
    tenant_id="00000000-0000-0000-0000-000000000001",
    entity_id="00000000-0000-0000-0000-000000000002",
    roles=["invoice_creator"],
))
')"

echo "=== Negative API and malformed-input tests ==="

expect_status 401 "missing JWT is rejected" \
  "$BASE_URL/api/v1/invoices/00000000-0000-0000-0000-000000000001"

expect_status 401 "malformed JWT is rejected" \
  "$BASE_URL/api/v1/invoices/00000000-0000-0000-0000-000000000001" \
  -H "Authorization: Bearer not-a-jwt"

expect_status 401 "wrong authorization scheme is rejected" \
  "$BASE_URL/api/v1/invoices/00000000-0000-0000-0000-000000000001" \
  -H "Authorization: Basic abc"

expect_status 400 "underscore header is rejected by Envoy" \
  -X POST "$BASE_URL/api/v1/invoices" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "X_Idempotency_Key: invalid-header-name" \
  -d '{}'

expect_status 422 "missing idempotency header is rejected" \
  -X POST "$BASE_URL/api/v1/invoices" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'

expect_status 422 "malformed JSON is rejected" \
  -X POST "$BASE_URL/api/v1/invoices" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: negative-malformed-json" \
  --data-binary '{"customer_id":'

expect_status 422 "JSON array cannot masquerade as an invoice" \
  -X POST "$BASE_URL/api/v1/invoices" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: negative-array-body" \
  -d '[]'

expect_status 422 "wrong content type is rejected" \
  -X POST "$BASE_URL/api/v1/invoices" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: text/plain" \
  -H "X-Idempotency-Key: negative-content-type" \
  -d 'not-json'

for attempt in $(seq 1 25); do
  expect_status 401 "malformed JWT burst $attempt" \
    "$BASE_URL/api/v1/journal-entries?invoice=invalid" \
    -H "Authorization: Bearer broken.$attempt.token"
done

expect_status 200 "server remains healthy after malformed-input burst" \
  "$BASE_URL/health"

echo "=== All negative API assertions passed ==="
