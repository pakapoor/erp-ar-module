#!/bin/bash
# Get invoice details.
# With arg: gets that invoice. No arg: gets both invoices from last create run.

SESSION=/Users/pankajkapoor/projects/erp-ar-module/debug/.debug_session
PROJECT=/Users/pankajkapoor/projects/erp-ar-module
BASE_URL="http://localhost:8000/api/v1"

if [ ! -f "$SESSION" ]; then
  echo "ERROR: No debug session. Run 1_create_invoice.sh first."
  exit 1
fi

TENANT_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['tenant_id'])")
ENTITY_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['entity_id'])")
CREATOR_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['user_creator_id'])")

cd "$PROJECT" || exit 1
TOKEN=$(docker compose exec -T app python -c "from src.auth import create_test_token; print(create_test_token('$CREATOR_ID','$TENANT_ID','$ENTITY_ID',['invoice_creator']))" 2>/dev/null)

get_invoice() {
  local INVOICE_ID=$1
  echo ""
  echo "=== Getting Invoice: $INVOICE_ID ==="
  curl -s --max-time 600 -X GET "$BASE_URL/invoices/$INVOICE_ID" \
    -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
}

if [ -n "$1" ]; then
  get_invoice "$1"
else
  INVOICE1=$(python3 -c "import json; print(json.load(open('$SESSION')).get('invoice1_id',''))")
  INVOICE2=$(python3 -c "import json; print(json.load(open('$SESSION')).get('invoice2_id',''))")
  if [ -z "$INVOICE1" ] && [ -z "$INVOICE2" ]; then
    echo "ERROR: No invoice IDs in session. Run 1_create_invoice.sh first."
    exit 1
  fi
  [ -n "$INVOICE1" ] && get_invoice "$INVOICE1"
  [ -n "$INVOICE2" ] && get_invoice "$INVOICE2"
fi