#!/bin/bash
# Create 2 tenants via the real POST /tenants API. Rotates the whole debug
# session -- run this first, then 0_create_entity.sh, then 1_create_invoice.sh.
#
# Bootstraps a one-time throwaway admin tenant (setup_bootstrap_admin.py) to
# get a system_admin JWT to call POST /tenants with -- see that script's
# docstring for why tenant creation can't be entirely self-hosted.

BASE_URL="http://localhost:8000/api/v1"
SESSION=/Users/pankajkapoor/projects/erp-ar-module/debug/.debug_session
BOOTSTRAP_SESSION=/Users/pankajkapoor/projects/erp-ar-module/debug/.bootstrap_session
PROJECT=/Users/pankajkapoor/projects/erp-ar-module

echo "=== Ensuring bootstrap admin exists ==="
cd "$PROJECT" || exit 1
docker compose cp debug/setup_bootstrap_admin.py app:/tmp/setup_bootstrap_admin.py || exit 1
docker compose exec -T app python /tmp/setup_bootstrap_admin.py || exit 1
docker compose cp app:/tmp/.bootstrap_session debug/.bootstrap_session || exit 1

BOOT_TENANT_ID=$(python3 -c "import json; print(json.load(open('$BOOTSTRAP_SESSION'))['tenant_id'])")
BOOT_ENTITY_ID=$(python3 -c "import json; print(json.load(open('$BOOTSTRAP_SESSION'))['entity_id'])")
BOOT_ADMIN_ID=$(python3 -c "import json; print(json.load(open('$BOOTSTRAP_SESSION'))['admin_user_id'])")

TOKEN=$(docker compose exec -T app python -c "from src.auth import create_test_token; print(create_test_token('$BOOT_ADMIN_ID','$BOOT_TENANT_ID','$BOOT_ENTITY_ID',['system_admin']))" 2>/dev/null)

create_tenant() {
  local NAME=$1
  local CURRENCY=$2
  local IDEM="debug-create-tenant-$(date +%s)-$RANDOM"
  curl -s --max-time 600 -X POST "$BASE_URL/tenants" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Idempotency-Key: $IDEM" \
    -d @- <<JSON
{
  "name": "$NAME",
  "base_currency": "$CURRENCY"
}
JSON
}

TIMESTAMP=$(date +%s)

echo ""
echo "=== Creating Tenant 1 (INR) ==="
RESP1=$(create_tenant "Debug Tenant A $TIMESTAMP" "INR")
echo "$RESP1" | python3 -m json.tool
TENANT1=$(echo "$RESP1" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','ERROR'))")
echo "Tenant 1 ID: $TENANT1"

sleep 1

echo ""
echo "=== Creating Tenant 2 (USD) ==="
RESP2=$(create_tenant "Debug Tenant B $TIMESTAMP" "USD")
echo "$RESP2" | python3 -m json.tool
TENANT2=$(echo "$RESP2" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','ERROR'))")
echo "Tenant 2 ID: $TENANT2"

python3 - "$SESSION" "$TENANT1" "$TENANT2" <<'PYEOF'
import json, sys
session, t1, t2 = sys.argv[1], sys.argv[2], sys.argv[3]
d = {
    "tenant1_id": t1,
    "tenant2_id": t2,
}
json.dump(d, open(session, "w"), indent=2)
PYEOF

echo ""
echo "=== Done ==="
echo "Tenant 1 (INR): $TENANT1"
echo "Tenant 2 (USD): $TENANT2"
echo "Next: ./0_create_entity.sh"
