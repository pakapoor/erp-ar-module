#!/bin/bash
# "Delete" a tenant. There is no hard DELETE in this API -- tenants (like
# every other resource here) are soft-deleted via PATCH .../is_active=false.
# With arg: deactivates that tenant. No arg: deactivates both session tenants.
# Usage: ./0_delete_tenant.sh [TENANT_ID]

BASE_URL="http://localhost:8000/api/v1"
SESSION=/Users/pankajkapoor/projects/erp-ar-module/debug/.debug_session
BOOTSTRAP_SESSION=/Users/pankajkapoor/projects/erp-ar-module/debug/.bootstrap_session
PROJECT=/Users/pankajkapoor/projects/erp-ar-module

if [ ! -f "$BOOTSTRAP_SESSION" ]; then
  echo "ERROR: No bootstrap admin session. Run 0_create_tenant.sh first."
  exit 1
fi

cd "$PROJECT" || exit 1

BOOT_TENANT_ID=$(python3 -c "import json; print(json.load(open('$BOOTSTRAP_SESSION'))['tenant_id'])")
BOOT_ENTITY_ID=$(python3 -c "import json; print(json.load(open('$BOOTSTRAP_SESSION'))['entity_id'])")
BOOT_ADMIN_ID=$(python3 -c "import json; print(json.load(open('$BOOTSTRAP_SESSION'))['admin_user_id'])")

TOKEN=$(docker compose exec -T app python -c "from src.auth import create_test_token; print(create_test_token('$BOOT_ADMIN_ID','$BOOT_TENANT_ID','$BOOT_ENTITY_ID',['system_admin']))" 2>/dev/null)

deactivate_tenant() {
  local TENANT_ID=$1
  echo ""
  echo "=== Deactivating Tenant: $TENANT_ID ==="

  local GET_RESP
  GET_RESP=$(curl -s --max-time 600 -X GET "$BASE_URL/tenants/$TENANT_ID" \
    -H "Authorization: Bearer $TOKEN")
  local VERSION
  VERSION=$(echo "$GET_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','ERROR'))" 2>/dev/null)

  if [ -z "$VERSION" ] || [ "$VERSION" = "ERROR" ]; then
    echo "ERROR: Could not fetch tenant $TENANT_ID"
    echo "$GET_RESP"
    return 1
  fi

  local IDEM="debug-delete-tenant-$TENANT_ID-$(date +%s)"
  curl -s --max-time 600 -X PATCH "$BASE_URL/tenants/$TENANT_ID" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Idempotency-Key: $IDEM" \
    -H "If-Match: $VERSION" \
    -d '{"is_active": false}' | python3 -m json.tool
}

if [ -n "$1" ]; then
  deactivate_tenant "$1"
else
  if [ ! -f "$SESSION" ]; then
    echo "ERROR: No debug session and no TENANT_ID given. Run 0_create_tenant.sh first, or pass a tenant ID explicitly."
    exit 1
  fi
  TENANT1=$(python3 -c "import json; print(json.load(open('$SESSION')).get('tenant1_id',''))")
  TENANT2=$(python3 -c "import json; print(json.load(open('$SESSION')).get('tenant2_id',''))")
  [ -n "$TENANT1" ] && deactivate_tenant "$TENANT1"
  [ -n "$TENANT2" ] && deactivate_tenant "$TENANT2"
fi

echo ""
echo "=== Done ==="
