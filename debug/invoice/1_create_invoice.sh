#!/bin/bash
# Create a new invoice
# Saves invoice_id to .last_invoice_id for use by other scripts

BASE_URL="http://localhost:8000/api/v1"
TOKEN=$(cd /Users/pankajkapoor/projects/erp-ar-module && docker compose exec -T app python -c "from src.auth import create_test_token; print(create_test_token('00000000-0000-0000-0000-000000000003','00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002',['invoice_creator']))" 2>/dev/null)

IDEM_KEY="debug-create-$(date +%s)"

echo "=== Creating Invoice ==="
RESPONSE=$(curl -s -X POST $BASE_URL/invoices 
  -H "Content-Type: application/json" 
  -H "Authorization: Bearer $TOKEN" 
  -H "X-Idempotency-Key: $IDEM_KEY" 
  -d '{
    "customer_id": "00000000-0000-0000-0000-000000000005",
    "invoice_date": "2026-07-21",
    "payment_terms": "NET30",
    "currency": "INR",
    "line_items": [
      {"description": "Safety Valves",     "quantity": 10, "unit_price": 10000, "tax_rate": 18},
      {"description": "Pressure Regulators","quantity": 5,  "unit_price": 8000,  "tax_rate": 18}
    ]
  }')

echo $RESPONSE | python3 -m json.tool

INVOICE_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','ERROR'))")
echo $INVOICE_ID > /Users/pankajkapoor/projects/erp-ar-module/debug/.last_invoice_id
echo ""
echo "Invoice ID: $INVOICE_ID"
echo "Saved to debug/.last_invoice_id"
