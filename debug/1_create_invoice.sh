#!/bin/bash
# Create 2 invoices for EACH entity in the session (8 total across the 2
# tenants x 2 entities from 0_create_tenant.sh / 0_create_entity.sh).
#
# For each entity, first seeds the users/GL accounts/period/customer that
# invoice creation still needs (setup_debug_session_for_entity.py -- there's
# no CRUD API for those yet), then creates 2 invoices via POST /invoices.

BASE_URL="http://localhost:8000/api/v1"
SESSION=/Users/pankajkapoor/projects/erp-ar-module/debug/.debug_session
PROJECT=/Users/pankajkapoor/projects/erp-ar-module

if [ ! -f "$SESSION" ]; then
  echo "ERROR: No debug session. Run 0_create_tenant.sh and 0_create_entity.sh first."
  exit 1
fi

TENANT1=$(python3 -c "import json; print(json.load(open('$SESSION')).get('tenant1_id',''))")
TENANT2=$(python3 -c "import json; print(json.load(open('$SESSION')).get('tenant2_id',''))")
E1A=$(python3 -c "import json; print(json.load(open('$SESSION')).get('tenant1_entity1_id',''))")
E1B=$(python3 -c "import json; print(json.load(open('$SESSION')).get('tenant1_entity2_id',''))")
E2A=$(python3 -c "import json; print(json.load(open('$SESSION')).get('tenant2_entity1_id',''))")
E2B=$(python3 -c "import json; print(json.load(open('$SESSION')).get('tenant2_entity2_id',''))")

if [ -z "$E1A" ] || [ -z "$E1B" ] || [ -z "$E2A" ] || [ -z "$E2B" ]; then
  echo "ERROR: Missing entity IDs in session. Run 0_create_tenant.sh then 0_create_entity.sh first."
  exit 1
fi

cd "$PROJECT" || exit 1
docker compose cp debug/setup_debug_session_for_entity.py app:/tmp/setup_debug_session_for_entity.py || exit 1

TODAY=$(date +%Y-%m-%d)

# seed_entity TENANT_ID ENTITY_ID -> prints JSON {creator_id, approver_id, payer_id, customer_id}
seed_entity() {
  local TENANT_ID=$1
  local ENTITY_ID=$2
  docker compose exec -T app python /tmp/setup_debug_session_for_entity.py "$TENANT_ID" "$ENTITY_ID" 2>/dev/null
}

# create_invoice TOKEN CUSTOMER_ID CURRENCY TERMS DESC QTY PRICE
create_invoice() {
  local TOKEN=$1 CUSTOMER_ID=$2 CURRENCY=$3 TERMS=$4 DESC=$5 QTY=$6 PRICE=$7
  local IDEM="debug-create-$TERMS-$(date +%s)-$RANDOM"
  curl -s --max-time 600 -X POST "$BASE_URL/invoices" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Idempotency-Key: $IDEM" \
    -d @- <<JSON
{
  "customer_id": "$CUSTOMER_ID",
  "invoice_date": "$TODAY",
  "payment_terms": "$TERMS",
  "currency": "$CURRENCY",
  "line_items": [
    {"description": "$DESC", "quantity": $QTY, "unit_price": $PRICE, "tax_rate": 18}
  ]
}
JSON
}

# process_entity LABEL TENANT_ID ENTITY_ID CURRENCY -> exports 2 invoice IDs via globals
process_entity() {
  local LABEL=$1 TENANT_ID=$2 ENTITY_ID=$3 CURRENCY=$4

  echo ""
  echo "=== $LABEL: seeding users/GL/period/customer ==="
  local SEED_JSON
  SEED_JSON=$(seed_entity "$TENANT_ID" "$ENTITY_ID")
  echo "$SEED_JSON" | python3 -m json.tool

  local CREATOR_ID APPROVER_ID PAYER_ID CUSTOMER_ID
  CREATOR_ID=$(echo "$SEED_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['creator_id'])")
  APPROVER_ID=$(echo "$SEED_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['approver_id'])")
  PAYER_ID=$(echo "$SEED_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['payer_id'])")
  CUSTOMER_ID=$(echo "$SEED_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['customer_id'])")

  local TOKEN
  TOKEN=$(docker compose exec -T app python -c "from src.auth import create_test_token; print(create_test_token('$CREATOR_ID','$TENANT_ID','$ENTITY_ID',['invoice_creator']))" 2>/dev/null)

  echo ""
  echo "=== $LABEL: creating Invoice 1 (Safety Valves, NET15) ==="
  local RESP1
  RESP1=$(create_invoice "$TOKEN" "$CUSTOMER_ID" "$CURRENCY" NET15 "Safety Valves" 10 10000)
  echo "$RESP1" | python3 -m json.tool
  INVOICE1=$(echo "$RESP1" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','ERROR'))")

  sleep 1

  echo ""
  echo "=== $LABEL: creating Invoice 2 (Pressure Regulators, NET30) ==="
  local RESP2
  RESP2=$(create_invoice "$TOKEN" "$CUSTOMER_ID" "$CURRENCY" NET30 "Pressure Regulators" 5 8000)
  echo "$RESP2" | python3 -m json.tool
  INVOICE2=$(echo "$RESP2" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','ERROR'))")

  LAST_LABEL_CREATOR_ID="$CREATOR_ID"
  LAST_LABEL_APPROVER_ID="$APPROVER_ID"
  LAST_LABEL_PAYER_ID="$PAYER_ID"
  LAST_LABEL_CUSTOMER_ID="$CUSTOMER_ID"
  LAST_LABEL_INVOICE1="$INVOICE1"
  LAST_LABEL_INVOICE2="$INVOICE2"
}

process_entity "Tenant 1 / Entity A" "$TENANT1" "$E1A" "INR"
T1A_CREATOR=$LAST_LABEL_CREATOR_ID; T1A_APPROVER=$LAST_LABEL_APPROVER_ID; T1A_PAYER=$LAST_LABEL_PAYER_ID
T1A_CUSTOMER=$LAST_LABEL_CUSTOMER_ID; T1A_INV1=$LAST_LABEL_INVOICE1; T1A_INV2=$LAST_LABEL_INVOICE2

process_entity "Tenant 1 / Entity B" "$TENANT1" "$E1B" "INR"
T1B_CREATOR=$LAST_LABEL_CREATOR_ID; T1B_APPROVER=$LAST_LABEL_APPROVER_ID; T1B_PAYER=$LAST_LABEL_PAYER_ID
T1B_CUSTOMER=$LAST_LABEL_CUSTOMER_ID; T1B_INV1=$LAST_LABEL_INVOICE1; T1B_INV2=$LAST_LABEL_INVOICE2

process_entity "Tenant 2 / Entity A" "$TENANT2" "$E2A" "USD"
T2A_CREATOR=$LAST_LABEL_CREATOR_ID; T2A_APPROVER=$LAST_LABEL_APPROVER_ID; T2A_PAYER=$LAST_LABEL_PAYER_ID
T2A_CUSTOMER=$LAST_LABEL_CUSTOMER_ID; T2A_INV1=$LAST_LABEL_INVOICE1; T2A_INV2=$LAST_LABEL_INVOICE2

process_entity "Tenant 2 / Entity B" "$TENANT2" "$E2B" "USD"
T2B_CREATOR=$LAST_LABEL_CREATOR_ID; T2B_APPROVER=$LAST_LABEL_APPROVER_ID; T2B_PAYER=$LAST_LABEL_PAYER_ID
T2B_CUSTOMER=$LAST_LABEL_CUSTOMER_ID; T2B_INV1=$LAST_LABEL_INVOICE1; T2B_INV2=$LAST_LABEL_INVOICE2

python3 - "$SESSION" \
  "$T1A_CREATOR" "$T1A_APPROVER" "$T1A_PAYER" "$T1A_CUSTOMER" "$T1A_INV1" "$T1A_INV2" \
  "$T1B_CREATOR" "$T1B_APPROVER" "$T1B_PAYER" "$T1B_CUSTOMER" "$T1B_INV1" "$T1B_INV2" \
  "$T2A_CREATOR" "$T2A_APPROVER" "$T2A_PAYER" "$T2A_CUSTOMER" "$T2A_INV1" "$T2A_INV2" \
  "$T2B_CREATOR" "$T2B_APPROVER" "$T2B_PAYER" "$T2B_CUSTOMER" "$T2B_INV1" "$T2B_INV2" <<'PYEOF'
import json, sys
(session,
 t1a_creator, t1a_approver, t1a_payer, t1a_customer, t1a_inv1, t1a_inv2,
 t1b_creator, t1b_approver, t1b_payer, t1b_customer, t1b_inv1, t1b_inv2,
 t2a_creator, t2a_approver, t2a_payer, t2a_customer, t2a_inv1, t2a_inv2,
 t2b_creator, t2b_approver, t2b_payer, t2b_customer, t2b_inv1, t2b_inv2) = sys.argv[1:26]

d = json.load(open(session))
d["tenant1_entity1_creator_id"] = t1a_creator
d["tenant1_entity1_approver_id"] = t1a_approver
d["tenant1_entity1_payer_id"] = t1a_payer
d["tenant1_entity1_customer_id"] = t1a_customer
d["tenant1_entity1_invoice1_id"] = t1a_inv1
d["tenant1_entity1_invoice2_id"] = t1a_inv2

d["tenant1_entity2_creator_id"] = t1b_creator
d["tenant1_entity2_approver_id"] = t1b_approver
d["tenant1_entity2_payer_id"] = t1b_payer
d["tenant1_entity2_customer_id"] = t1b_customer
d["tenant1_entity2_invoice1_id"] = t1b_inv1
d["tenant1_entity2_invoice2_id"] = t1b_inv2

d["tenant2_entity1_creator_id"] = t2a_creator
d["tenant2_entity1_approver_id"] = t2a_approver
d["tenant2_entity1_payer_id"] = t2a_payer
d["tenant2_entity1_customer_id"] = t2a_customer
d["tenant2_entity1_invoice1_id"] = t2a_inv1
d["tenant2_entity1_invoice2_id"] = t2a_inv2

d["tenant2_entity2_creator_id"] = t2b_creator
d["tenant2_entity2_approver_id"] = t2b_approver
d["tenant2_entity2_payer_id"] = t2b_payer
d["tenant2_entity2_customer_id"] = t2b_customer
d["tenant2_entity2_invoice1_id"] = t2b_inv1
d["tenant2_entity2_invoice2_id"] = t2b_inv2

# Legacy single-invoice keys used by 2_get_invoice.sh etc. -- point at the
# last entity processed (Tenant 2 / Entity B) so the existing numbered
# scripts keep working with no-arg defaults.
d["tenant_id"] = d["tenant2_id"]
d["entity_id"] = d["tenant2_entity2_id"]
d["user_creator_id"] = t2b_creator
d["user_approver_id"] = t2b_approver
d["user_payer_id"] = t2b_payer
d["customer_id"] = t2b_customer
d["invoice1_id"] = t2b_inv1
d["invoice2_id"] = t2b_inv2
d["last_invoice_id"] = t2b_inv2

json.dump(d, open(session, "w"), indent=2)
PYEOF
echo "$T2B_INV2" > "$PROJECT/debug/.last_invoice_id"

echo ""
echo "=== Done: 8 invoices created across 2 tenants x 2 entities ==="
echo "Tenant 1 / Entity A: $T1A_INV1, $T1A_INV2"
echo "Tenant 1 / Entity B: $T1B_INV1, $T1B_INV2"
echo "Tenant 2 / Entity A: $T2A_INV1, $T2A_INV2"
echo "Tenant 2 / Entity B: $T2B_INV1, $T2B_INV2"
echo ""
echo "Note: user_creator_id/user_approver_id/user_payer_id for the scripts"
echo "below (2_get_invoice.sh etc.) now default to Tenant 2 / Entity B."
echo "Next: ./3_approve_invoice.sh"
