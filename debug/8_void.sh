#!/bin/bash
# Void an invoice. Allowed against DRAFT, APPROVED, SENT.
# With arg: voids that invoice. No arg: uses last invoice from session.
# Usage: ./8_void.sh [INVOICE_ID] [REASON_CODE]

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

INVOICE_ID=${1:-}
if [ -z "$INVOICE_ID" ]; then
  INVOICE_ID=$(python3 -c "import json; print(json.load(open('$SESSION')).get('last_invoice_id','038c6b70-5353-4447-b33e-d957c92199fd'))")
fi

REASON_CODE=${2:-DATA_ERROR}
IDEM_KEY="debug-void-$INVOICE_ID-$(date +%s)"

echo ""
echo "=== Voiding Invoice: $INVOICE_ID ==="
echo "Reason:      $REASON_CODE"
echo ""

curl -s --max-time 600 -X POST "$BASE_URL/invoices/$INVOICE_ID/void" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Idempotency-Key: $IDEM_KEY" \
  -d @- <<JSON | python3 -m json.tool
{
  "reason_code": "$REASON_CODE",
  "description": "Voided in debug session",
  "void_reference": ""
}
JSON
