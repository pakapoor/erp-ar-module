#!/bin/bash
# Edit a DRAFT invoice (creator only; covers fresh drafts and rejected invoices).
# Usage: ./3_1_patch_invoice.sh <INVOICE_ID> [description] [quantity] [unit_price]

SESSION=/Users/pankajkapoor/projects/erp-ar-module/debug/.debug_session
PROJECT=/Users/pankajkapoor/projects/erp-ar-module
BASE_URL="http://localhost:8000/api/v1"

if [ ! -f "$SESSION" ]; then
  echo "ERROR: No debug session. Run 1_create_invoice.sh first."
  exit 1
fi

INVOICE_ID=${1:-}
if [ -z "$INVOICE_ID" ]; then
  echo "ERROR: Invoice ID is required."
  echo "Usage: ./3_1_patch_invoice.sh <INVOICE_ID> [description] [quantity] [unit_price]"
  exit 1
fi

DESCRIPTION=${2:-"Corrected line item"}
QUANTITY=${3:-10}
UNIT_PRICE=${4:-9500}

TENANT_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['tenant_id'])")
ENTITY_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['entity_id'])")
CREATOR_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['user_creator_id'])")

cd "$PROJECT" || exit 1
TOKEN=$(docker compose exec -T app python -c "from src.auth import create_test_token; print(create_test_token('$CREATOR_ID','$TENANT_ID','$ENTITY_ID',['invoice_creator']))" 2>/dev/null)

# Fetch current version — invoice may be at v1 (fresh draft) or higher (rejected/edited).
VERSION=$(curl -s "$BASE_URL/invoices/$INVOICE_ID" -H "Authorization: Bearer $TOKEN" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])")
IDEM="debug-patch-$INVOICE_ID-$(date +%s)"

echo ""
echo "=== Patching Invoice: $INVOICE_ID (version $VERSION) ==="
curl -s --max-time 600 -X PATCH "$BASE_URL/invoices/$INVOICE_ID" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Idempotency-Key: $IDEM" \
  -H "If-Match: $VERSION" \
  -d @- <<JSON | python3 -m json.tool
{
  "line_items": [
    {"description": "$DESCRIPTION", "quantity": $QUANTITY, "unit_price": $UNIT_PRICE, "tax_rate": 18}
  ]
}
JSON
