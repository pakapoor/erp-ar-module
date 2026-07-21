#!/bin/bash
# Approve invoice (Priya approves)
# Reads invoice_id from .last_invoice_id or pass as argument

BASE_URL="http://localhost:8000/api/v1"
TOKEN=$(cd /Users/pankajkapoor/projects/erp-ar-module && docker compose exec -T app python -c "from src.auth import create_test_token; print(create_test_token('00000000-0000-0000-0000-000000000004','00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002',['invoice_approver','cfo']))" 2>/dev/null)

INVOICE_ID=${1:-$(cat /Users/pankajkapoor/projects/erp-ar-module/debug/.last_invoice_id 2>/dev/null)}
VERSION=${2:-1}
IDEM_KEY="debug-approve-$(date +%s)"

if [ -z "$INVOICE_ID" ] || [ "$INVOICE_ID" = "ERROR" ]; then
  echo "ERROR: No invoice ID. Run 1_create_invoice.sh first or pass ID as argument."
  exit 1
fi

echo "=== Approving Invoice: $INVOICE_ID (version $VERSION) ==="
echo "Expected GL entry:"
echo "  Dr 1200 Accounts Receivable"
echo "  Cr 3100 Sales Revenue"
echo "  Cr 2200 Tax Payable"
echo ""

curl -s -X POST $BASE_URL/invoices/$INVOICE_ID/approve 
  -H "Content-Type: application/json" 
  -H "Authorization: Bearer $TOKEN" 
  -H "X-Idempotency-Key: $IDEM_KEY" 
  -H "If-Match: $VERSION" 
  -d '{"notes": "Approved in debug session"}' | python3 -m json.tool
