#!/bin/bash
# Create 2 invoices for a fresh debug session. Runs setup automatically.

BASE_URL="http://localhost:8000/api/v1"
SESSION=/Users/pankajkapoor/projects/erp-ar-module/debug/.debug_session
PROJECT=/Users/pankajkapoor/projects/erp-ar-module

echo "=== Setting up fresh debug session ==="
cd "$PROJECT" || exit 1
docker compose cp debug/setup_debug_session.py app:/tmp/setup_debug_session.py || exit 1
docker compose exec -T app python /tmp/setup_debug_session.py || exit 1
docker compose cp app:/tmp/.debug_session debug/.debug_session || exit 1

TENANT_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['tenant_id'])")
ENTITY_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['entity_id'])")
CUSTOMER_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['customer_id'])")
CREATOR_ID=$(python3 -c "import json; print(json.load(open('$SESSION'))['user_creator_id'])")

TOKEN=$(docker compose exec -T app python -c "from src.auth import create_test_token; print(create_test_token('$CREATOR_ID','$TENANT_ID','$ENTITY_ID',['invoice_creator']))" 2>/dev/null)
TODAY=$(date +%Y-%m-%d)

create_invoice() {
  local TERMS=$1
  local DESC=$2
  local QTY=$3
  local PRICE=$4
  local IDEM="debug-create-$TERMS-$(date +%s)"
  curl -s --max-time 600 -X POST "$BASE_URL/invoices" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Idempotency-Key: $IDEM" \
    -d @- <<JSON
{
  "customer_id": "$CUSTOMER_ID",
  "invoice_date": "$TODAY",
  "payment_terms": "$TERMS",
  "currency": "INR",
  "line_items": [
    {"description": "$DESC", "quantity": $QTY, "unit_price": $PRICE, "tax_rate": 18}
  ]
}
JSON
}

echo ""
echo "=== Creating Invoice 1 (Safety Valves, NET15) ==="
RESP1=$(create_invoice NET15 "Safety Valves" 10 10000)
echo "$RESP1" | python3 -m json.tool
INVOICE1=$(echo "$RESP1" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','ERROR'))")
echo "Invoice 1 ID: $INVOICE1"

sleep 1

echo ""
echo "=== Creating Invoice 2 (Pressure Regulators, NET30) ==="
RESP2=$(create_invoice NET30 "Pressure Regulators" 5 8000)
echo "$RESP2" | python3 -m json.tool
INVOICE2=$(echo "$RESP2" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','ERROR'))")
echo "Invoice 2 ID: $INVOICE2"

python3 - "$SESSION" "$INVOICE1" "$INVOICE2" <<'PYEOF'
import json, sys
session, inv1, inv2 = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(session))
d["invoice1_id"] = inv1
d["invoice2_id"] = inv2
d["last_invoice_id"] = inv2
json.dump(d, open(session, "w"), indent=2)
PYEOF
echo "$INVOICE2" > "$PROJECT/debug/.last_invoice_id"

echo ""
echo "=== Done ==="
echo "Invoice 1 (NET15, FIFO first):  $INVOICE1"
echo "Invoice 2 (NET30, FIFO second): $INVOICE2"
echo "Next: ./3_approve_invoice.sh $INVOICE1"