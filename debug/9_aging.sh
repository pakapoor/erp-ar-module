#!/bin/bash
# Get AR aging report for a customer.
# With arg: gets aging for that customer. No arg: uses customer from session.
# Usage: ./9_aging.sh [CUSTOMER_ID]

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

CUSTOMER_ID=${1:-}
if [ -z "$CUSTOMER_ID" ]; then
  CUSTOMER_ID=$(python3 -c "import json; print(json.load(open('$SESSION')).get('customer_id','383f7a72-b4f2-4a21-88b4-7487ea06f655'))")
fi

echo ""
echo "=== AR Aging Report for Customer: $CUSTOMER_ID ==="
echo ""

curl -s --max-time 600 -X GET "$BASE_URL/customers/$CUSTOMER_ID/aging" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
