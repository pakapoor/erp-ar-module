#!/bin/bash
# Full CRUD lifecycle for POST /tenants and POST /entities against the live
# stack: create, get, list, patch (deactivate/reactivate), and the guardrails
# specific to these two resources (entity always scoped to the caller's own
# tenant, parent-entity validation, name uniqueness, optimistic locking).

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR"

BASE_URL="${BASE_URL:-http://localhost:8000}/api/v1"

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

new_uuid() {
  python3 -c 'import uuid; print(uuid.uuid4())'
}

echo "=== Setup: seed data + bootstrap admin tenant/entity for JWTs ==="
docker compose exec -T app python -m src.seed_data >/dev/null

docker compose cp debug/setup_bootstrap_admin.py app:/tmp/setup_bootstrap_admin.py >/dev/null
BOOTSTRAP_JSON="$(docker compose exec -T app python /tmp/setup_bootstrap_admin.py 2>/dev/null | python3 -c 'import sys,json; buf=sys.stdin.read(); start=buf.index("{"); end=buf.rindex("}")+1; print(buf[start:end])')"

BOOT_TENANT_ID="$(python3 -c "import json; print(json.loads('''$BOOTSTRAP_JSON''')['tenant_id'])")"
BOOT_ENTITY_ID="$(python3 -c "import json; print(json.loads('''$BOOTSTRAP_JSON''')['entity_id'])")"
BOOT_ADMIN_ID="$(python3 -c "import json; print(json.loads('''$BOOTSTRAP_JSON''')['admin_user_id'])")"

ADMIN_TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token('$BOOT_ADMIN_ID','$BOOT_TENANT_ID','$BOOT_ENTITY_ID',['system_admin']))
" 2>/dev/null)"

NON_ADMIN_TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token('$BOOT_ADMIN_ID','$BOOT_TENANT_ID','$BOOT_ENTITY_ID',['invoice_creator']))
" 2>/dev/null)"

echo ""
echo "=== POST /tenants ==="

expect_status 403 "non-system_admin cannot create a tenant" \
  -X POST "$BASE_URL/tenants" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $NON_ADMIN_TOKEN" \
  -H "X-Idempotency-Key: itest-tenant-forbidden-$(new_uuid)" \
  -d '{"name": "Should Not Exist", "base_currency": "USD"}'

expect_status 422 "creating a tenant requires the X-Idempotency-Key header" \
  -X POST "$BASE_URL/tenants" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"name": "Should Not Exist Either", "base_currency": "USD"}'

TENANT_NAME="ITest Tenant $(date +%s)-$RANDOM"
IDEM_KEY="itest-tenant-create-$(new_uuid)"
CREATE_TENANT_RESP="$(curl -s -X POST "$BASE_URL/tenants" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "X-Idempotency-Key: $IDEM_KEY" \
  -d "{\"name\": \"$TENANT_NAME\", \"base_currency\": \"inr\"}")"

assert_json "$CREATE_TENANT_RESP" 'data["base_currency"] == "INR"' \
  "tenant create normalizes currency to uppercase"
assert_json "$CREATE_TENANT_RESP" 'data["is_active"] is True and data["version"] == 1' \
  "new tenant is active with version 1"

TENANT_ID="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['id'])" "$CREATE_TENANT_RESP")"

REPLAY_RESP="$(curl -s -X POST "$BASE_URL/tenants" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "X-Idempotency-Key: $IDEM_KEY" \
  -d "{\"name\": \"$TENANT_NAME\", \"base_currency\": \"inr\"}")"
assert_json "$REPLAY_RESP" "data[\"id\"] == \"$TENANT_ID\"" \
  "replaying the same idempotency key returns the same tenant, not a duplicate"

expect_status 409 "duplicate tenant name is rejected" \
  -X POST "$BASE_URL/tenants" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "X-Idempotency-Key: itest-tenant-dup-$(new_uuid)" \
  -d "{\"name\": \"$TENANT_NAME\", \"base_currency\": \"USD\"}"

echo ""
echo "=== GET /tenants/{id}, GET /tenants ==="

GET_TENANT_RESP="$(curl -s -X GET "$BASE_URL/tenants/$TENANT_ID" -H "Authorization: Bearer $ADMIN_TOKEN")"
assert_json "$GET_TENANT_RESP" "data[\"id\"] == \"$TENANT_ID\"" "GET tenant returns the created tenant"

expect_status 404 "GET on a nonexistent tenant is 404" \
  -X GET "$BASE_URL/tenants/$(new_uuid)" -H "Authorization: Bearer $ADMIN_TOKEN"

LIST_RESP="$(curl -s -X GET "$BASE_URL/tenants" -H "Authorization: Bearer $ADMIN_TOKEN")"
assert_json "$LIST_RESP" "\"$TENANT_ID\" in [t[\"id\"] for t in data[\"tenants\"]]" \
  "tenant list includes the newly created tenant"

echo ""
echo "=== PATCH /tenants/{id} (optimistic locking) ==="

expect_status 409 "PATCH with a stale If-Match version is rejected" \
  -X PATCH "$BASE_URL/tenants/$TENANT_ID" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "X-Idempotency-Key: itest-tenant-patch-stale-$(new_uuid)" \
  -H "If-Match: 999" \
  -d '{"is_active": false}'

PATCH_RESP="$(curl -s -X PATCH "$BASE_URL/tenants/$TENANT_ID" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "X-Idempotency-Key: itest-tenant-patch-$(new_uuid)" \
  -H "If-Match: 1" \
  -d '{"is_active": false}')"
assert_json "$PATCH_RESP" 'data["is_active"] is False and data["version"] == 2' \
  "PATCH deactivates the tenant and bumps version to 2"

echo ""
echo "=== POST /entities ==="

expect_status 404 "creating an entity for an inactive tenant is rejected" \
  -X POST "$BASE_URL/entities" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token('$(new_uuid)','$TENANT_ID','$TENANT_ID',['system_admin']))
" 2>/dev/null)" \
  -H "X-Idempotency-Key: itest-entity-inactive-tenant-$(new_uuid)" \
  -d '{"name": "Should Not Exist", "currency": "INR"}'

# Reactivate the tenant for the rest of the entity tests.
REACTIVATE_RESP="$(curl -s -X PATCH "$BASE_URL/tenants/$TENANT_ID" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "X-Idempotency-Key: itest-tenant-reactivate-$(new_uuid)" \
  -H "If-Match: 2" \
  -d '{"is_active": true}')"
assert_json "$REACTIVATE_RESP" 'data["is_active"] is True' "tenant reactivated for entity tests"

TENANT_ADMIN_TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token('$(new_uuid)','$TENANT_ID','$TENANT_ID',['system_admin']))
" 2>/dev/null)"

ENTITY_NAME="ITest Entity $(date +%s)-$RANDOM"
ENTITY_IDEM_KEY="itest-entity-create-$(new_uuid)"
CREATE_ENTITY_RESP="$(curl -s -X POST "$BASE_URL/entities" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TENANT_ADMIN_TOKEN" \
  -H "X-Idempotency-Key: $ENTITY_IDEM_KEY" \
  -d "{\"name\": \"$ENTITY_NAME\", \"currency\": \"usd\"}")"

assert_json "$CREATE_ENTITY_RESP" "data[\"tenant_id\"] == \"$TENANT_ID\"" \
  "entity is created under the caller's own tenant (from JWT, not client input)"
assert_json "$CREATE_ENTITY_RESP" 'data["currency"] == "USD"' \
  "entity create normalizes currency to uppercase"

ENTITY_ID="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['id'])" "$CREATE_ENTITY_RESP")"

REPLAY_ENTITY_RESP="$(curl -s -X POST "$BASE_URL/entities" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TENANT_ADMIN_TOKEN" \
  -H "X-Idempotency-Key: $ENTITY_IDEM_KEY" \
  -d "{\"name\": \"$ENTITY_NAME\", \"currency\": \"usd\"}")"
assert_json "$REPLAY_ENTITY_RESP" "data[\"id\"] == \"$ENTITY_ID\"" \
  "replaying the same idempotency key returns the same entity, not a duplicate"

expect_status 409 "duplicate entity name within the same tenant is rejected" \
  -X POST "$BASE_URL/entities" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TENANT_ADMIN_TOKEN" \
  -H "X-Idempotency-Key: itest-entity-dup-$(new_uuid)" \
  -d "{\"name\": \"$ENTITY_NAME\", \"currency\": \"USD\"}"

expect_status 422 "parent_entity_id from a nonexistent entity is rejected" \
  -X POST "$BASE_URL/entities" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TENANT_ADMIN_TOKEN" \
  -H "X-Idempotency-Key: itest-entity-badparent-$(new_uuid)" \
  -d "{\"name\": \"Orphan Child $(new_uuid)\", \"currency\": \"USD\", \"parent_entity_id\": \"$(new_uuid)\"}"

CHILD_ENTITY_RESP="$(curl -s -X POST "$BASE_URL/entities" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TENANT_ADMIN_TOKEN" \
  -H "X-Idempotency-Key: itest-entity-child-$(new_uuid)" \
  -d "{\"name\": \"Child Entity $(new_uuid)\", \"currency\": \"USD\", \"parent_entity_id\": \"$ENTITY_ID\"}")"
assert_json "$CHILD_ENTITY_RESP" "data[\"parent_entity_id\"] == \"$ENTITY_ID\"" \
  "child entity records its parent"

echo ""
echo "=== GET /entities/{id}, GET /entities ==="

GET_ENTITY_RESP="$(curl -s -X GET "$BASE_URL/entities/$ENTITY_ID" -H "Authorization: Bearer $TENANT_ADMIN_TOKEN")"
assert_json "$GET_ENTITY_RESP" "data[\"id\"] == \"$ENTITY_ID\"" "GET entity returns the created entity"

expect_status 404 "GET on a nonexistent entity is 404" \
  -X GET "$BASE_URL/entities/$(new_uuid)" -H "Authorization: Bearer $TENANT_ADMIN_TOKEN"

LIST_ENTITIES_RESP="$(curl -s -X GET "$BASE_URL/entities" -H "Authorization: Bearer $TENANT_ADMIN_TOKEN")"
assert_json "$LIST_ENTITIES_RESP" "\"$ENTITY_ID\" in [e[\"id\"] for e in data[\"entities\"]]" \
  "entity list includes the newly created entity"

echo ""
echo "=== PATCH /entities/{id} (optimistic locking + self-parent guard) ==="

expect_status 422 "an entity cannot be set as its own parent" \
  -X PATCH "$BASE_URL/entities/$ENTITY_ID" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TENANT_ADMIN_TOKEN" \
  -H "X-Idempotency-Key: itest-entity-selfparent-$(new_uuid)" \
  -H "If-Match: 1" \
  -d "{\"parent_entity_id\": \"$ENTITY_ID\"}"

expect_status 409 "PATCH with a stale If-Match version is rejected" \
  -X PATCH "$BASE_URL/entities/$ENTITY_ID" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TENANT_ADMIN_TOKEN" \
  -H "X-Idempotency-Key: itest-entity-patch-stale-$(new_uuid)" \
  -H "If-Match: 999" \
  -d '{"is_active": false}'

PATCH_ENTITY_RESP="$(curl -s -X PATCH "$BASE_URL/entities/$ENTITY_ID" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TENANT_ADMIN_TOKEN" \
  -H "X-Idempotency-Key: itest-entity-patch-$(new_uuid)" \
  -H "If-Match: 1" \
  -d '{"is_active": false}')"
assert_json "$PATCH_ENTITY_RESP" 'data["is_active"] is False and data["version"] == 2' \
  "PATCH deactivates the entity and bumps version to 2"

echo ""
echo "=== Cross-tenant isolation ==="

OTHER_TENANT_IDEM="itest-other-tenant-$(new_uuid)"
OTHER_TENANT_RESP="$(curl -s -X POST "$BASE_URL/tenants" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "X-Idempotency-Key: $OTHER_TENANT_IDEM" \
  -d "{\"name\": \"ITest Other Tenant $(date +%s)-$RANDOM\", \"base_currency\": \"EUR\"}")"
OTHER_TENANT_ID="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['id'])" "$OTHER_TENANT_RESP")"
OTHER_TENANT_TOKEN="$(docker compose exec -T app python -c "
from src.auth import create_test_token
print(create_test_token('$(new_uuid)','$OTHER_TENANT_ID','$OTHER_TENANT_ID',['system_admin']))
" 2>/dev/null)"

expect_status 404 "a tenant's entity is invisible to a different tenant's admin" \
  -X GET "$BASE_URL/entities/$ENTITY_ID" -H "Authorization: Bearer $OTHER_TENANT_TOKEN"

echo ""
echo "=== All tenant/entity API assertions passed ==="
