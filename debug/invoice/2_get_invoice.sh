#!/bin/bash
# Get invoice by ID
# Reads invoice_id from .last_invoice_id or pass as argument: ./2_get_invoice.sh <id>

BASE_URL="http://localhost:8000/api/v1"
TOKEN=$(cd /Users/pankajkapoor/projects/erp-ar-module && docker compose exec -T app python -c "from src.auth import create_test_token; print(create_test_token('00000000-0000-0000-0000-000000000004','00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002',['invoice_approver','cfo']))" 2>/dev/null)

INVOICE_ID=${1:-$(cat /Users/pankajkapoor/projects/erp-ar-module/debug/.last_invoice_id 2>/dev/null)}

if [ -z "$INVOICE_ID" ] || [ "$INVOICE_ID" = "ERROR" ]; then
  echo "ERROR: No invoice ID. Run 1_create_invoice.sh first or pass ID as argument."
  exit 1
fi

echo "=== Getting Invoice: $INVOICE_ID ==="
curl -s -X GET $BASE_URL/invoices/$INVOICE_ID \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
