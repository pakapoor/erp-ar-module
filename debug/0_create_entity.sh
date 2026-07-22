#!/bin/bash
# Create 2 entities for EACH tenant in the session (4 total) via the real
# POST /entities API. Run after 0_create_tenant.sh.
#
# An entity is always created for the caller's own tenant (derived from the
# JWT, never accepted from the client) -- so this mints one admin token per
# tenant with that tenant's ID as the JWT's tenant_id claim.

BASE_URL="http://localhost:8000/api/v1"
SESSION=/Users/pankajkapoor/projects/erp-ar-module/debug/.debug_session
PROJECT=/Users/pankajkapoor/projects/erp-ar-module

if [ ! -f "$SESSION" ]; then
  echo "ERROR: No debug session. Run 0_create_tenant.sh first."
  exit 1
fi

TENANT1=$(python3 -c "import json; print(json.load(open('$SESSION')).get('tenant1_id',''))")
TENANT2=$(python3 -c "import json; print(json.load(open('$SESSION')).get('tenant2_id',''))")

if [ -z "$TENANT1" ] || [ -z "$TENANT2" ]; then
  echo "ERROR: No tenant IDs in session. Run 0_create_tenant.sh first."
  exit 1
fi

cd "$PROJECT" || exit 1

# create_entity_for_tenant TENANT_ID CURRENCY NAME
create_entity_for_tenant() {
  local TENANT_ID=$1
  local CURRENCY=$2
  local NAME=$3
  local ADMIN_ID
  ADMIN_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
  local TOKEN
  TOKEN=$(docker compose exec -T app python -c "from src.auth import create_test_token; print(create_test_token('$ADMIN_ID','$TENANT_ID','$TENANT_ID',['system_admin']))" 2>/dev/null)
  local IDEM="debug-create-entity-$(date +%s)-$RANDOM"

  curl -s --max-time 600 -X POST "$BASE_URL/entities" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Idempotency-Key: $IDEM" \
    -d @- <<JSON
{
  "name": "$NAME",
  "currency": "$CURRENCY"
}
JSON
}

TIMESTAMP=$(date +%s)

echo ""
echo "=== Tenant 1 ($TENANT1): creating 2 entities ==="
RESP1A=$(create_entity_for_tenant "$TENANT1" "INR" "Tenant A Entity 1 $TIMESTAMP")
echo "$RESP1A" | python3 -m json.tool
ENTITY1A=$(echo "$RESP1A" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','ERROR'))")

sleep 1

RESP1B=$(create_entity_for_tenant "$TENANT1" "INR" "Tenant A Entity 2 $TIMESTAMP")
echo "$RESP1B" | python3 -m json.tool
ENTITY1B=$(echo "$RESP1B" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','ERROR'))")

echo ""
echo "=== Tenant 2 ($TENANT2): creating 2 entities ==="
RESP2A=$(create_entity_for_tenant "$TENANT2" "USD" "Tenant B Entity 1 $TIMESTAMP")
echo "$RESP2A" | python3 -m json.tool
ENTITY2A=$(echo "$RESP2A" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','ERROR'))")

sleep 1

RESP2B=$(create_entity_for_tenant "$TENANT2" "USD" "Tenant B Entity 2 $TIMESTAMP")
echo "$RESP2B" | python3 -m json.tool
ENTITY2B=$(echo "$RESP2B" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','ERROR'))")

python3 - "$SESSION" "$ENTITY1A" "$ENTITY1B" "$ENTITY2A" "$ENTITY2B" <<'PYEOF'
import json, sys
session, e1a, e1b, e2a, e2b = sys.argv[1:6]
d = json.load(open(session))
d["tenant1_entity1_id"] = e1a
d["tenant1_entity2_id"] = e1b
d["tenant2_entity1_id"] = e2a
d["tenant2_entity2_id"] = e2b
json.dump(d, open(session, "w"), indent=2)
PYEOF

echo ""
echo "=== Done ==="
echo "Tenant 1 entities: $ENTITY1A, $ENTITY1B"
echo "Tenant 2 entities: $ENTITY2A, $ENTITY2B"
echo "Next: ./1_create_invoice.sh"
