#!/bin/bash
# Approve invoice(s).
# With arg: approves that invoice only.
# No arg:   approves the invoices created by the last 1_create_invoice.sh run.

SESSION=/Users/pankajkapoor/projects/erp-ar-module/debug/.debug_session
PROJECT=/Users/pankajkapoor/projects/erp-ar-module
BASE_URL="http://localhost:8000/api/v1"

if [ ! -f "$SESSION" ]; then
  echo "ERROR: No debug session. Run 1_create_invoice.sh first."
  exit 1
fi

TENANT_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['tenant_id'])")
ENTITY_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['entity_id'])")
APPROVER_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['user_approver_id'])")

cd "$PROJECT" || exit 1
TOKEN=$(docker compose exec -T app python -c "from src.auth import create_test_token; print(create_test_token('$APPROVER_ID','$TENANT_ID','$ENTITY_ID',['invoice_approver','cfo']))" 2>/dev/null)

approve_invoice() {
  local INVOICE_ID=$1
  local IDEM="debug-approve-$INVOICE_ID-$(date +%s)"
  local VERSION
  VERSION=$(curl -s "$BASE_URL/invoices/$INVOICE_ID" -H "Authorization: Bearer $TOKEN" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])")
  echo ""
  echo "=== Approving Invoice: $INVOICE_ID (version $VERSION) ==="
  curl -s --max-time 600 -X POST "$BASE_URL/invoices/$INVOICE_ID/approve" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Idempotency-Key: $IDEM" \
    -H "If-Match: $VERSION" \
    -d '{"notes": "Approved in debug session"}' | python3 -m json.tool
}

if [ -n "$1" ]; then
  approve_invoice "$1"
else
  INVOICE1=$(python3 -c "import json; print(json.load(open('$SESSION')).get('invoice1_id',''))")
  INVOICE2=$(python3 -c "import json; print(json.load(open('$SESSION')).get('invoice2_id',''))")
  if [ -z "$INVOICE1" ] && [ -z "$INVOICE2" ]; then
    echo "ERROR: No invoice IDs in session. Run 1_create_invoice.sh first."
    exit 1
  fi
  [ -n "$INVOICE1" ] && approve_invoice "$INVOICE1"
  [ -n "$INVOICE2" ] && approve_invoice "$INVOICE2"
fi