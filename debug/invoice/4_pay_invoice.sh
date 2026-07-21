#!/bin/bash
# Record payment for the debug session customer. Amount required.
# Usage: ./4_pay_invoice.sh <amount>
# AUTO/FIFO allocation: pays oldest due date first.

SESSION=/Users/pankajkapoor/projects/erp-ar-module/debug/.debug_session
PROJECT=/Users/pankajkapoor/projects/erp-ar-module
BASE_URL="http://localhost:8000/api/v1"

if [ ! -f "$SESSION" ]; then
  echo "ERROR: No debug session. Run 1_create_invoice.sh first."
  exit 1
fi

AMOUNT=${1:-}
if [ -z "$AMOUNT" ]; then
  echo "ERROR: Amount is required."
  echo "Usage: ./4_pay_invoice.sh <amount>"
  echo "Example: ./4_pay_invoice.sh 118000"
  exit 1
fi

TENANT_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['tenant_id'])")
ENTITY_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['entity_id'])")
CUSTOMER_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['customer_id'])")
PAYER_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['user_payer_id'])")

cd "$PROJECT" || exit 1
TOKEN=$(docker compose exec -T app python -c "from src.auth import create_test_token; print(create_test_token('$PAYER_ID','$TENANT_ID','$ENTITY_ID',['payment_recorder','cfo']))" 2>/dev/null)

PAYMENT_DATE=$(date +%Y-%m-%d)
PAYMENT_REF="DEBUG-PAY-$(date +%s)"
IDEM_KEY="debug-pay-$(date +%s)"

echo "=== Recording Payment ==="
echo "Customer: $CUSTOMER_ID"
echo "Amount:   $AMOUNT INR"
echo "Date:     $PAYMENT_DATE"
echo "Mode:     AUTO (FIFO -- oldest due date first)"
echo ""

curl -s --max-time 600 -X POST "$BASE_URL/payments" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Idempotency-Key: $IDEM_KEY" \
  -d @- <<JSON | python3 -m json.tool
{
  "customer_id": "$CUSTOMER_ID",
  "payment_reference": "$PAYMENT_REF",
  "payment_date": "$PAYMENT_DATE",
  "currency": "INR",
  "amount": $AMOUNT,
  "payment_method": "NEFT",
  "allocation_mode": "AUTO"
}
JSON