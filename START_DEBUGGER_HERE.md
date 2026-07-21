# 🎯 START HERE: POST /invoices Debugger Setup

## What You Have

I've annotated the **POST /invoices** endpoint with **20 strategic breakpoints** marked with 🔴 comments.

```
src/routers/invoices.py - Lines 228-470
```

---

## Step 1: Open the Code in VS Code

1. **Open** VS Code
2. **Go to** `src/routers/invoices.py`
3. **Search** for "BREAKPOINT 1" (Ctrl+F / Cmd+F)
4. You'll see: `# 🔴 BREAKPOINT 1: Entry point - request received`

---

## Step 2: Set Breakpoints

Click on any line number to set a breakpoint (red dot appears).

**Recommended starting breakpoints:**
- Line 230 (🔴 BREAKPOINT 1) - Entry
- Line 277 (🔴 BREAKPOINT 5) - Inspect customer
- Line 307 (🔴 BREAKPOINT 8) - Check totals
- Line 337 (🔴 BREAKPOINT 12) - Credit limit
- Line 343 (🔴 BREAKPOINT 13) - Create invoice
- Line 368 (🔴 BREAKPOINT 14) - After flush (invoice.id assigned)
- Line 408 (🔴 BREAKPOINT 17) - Response object
- Line 466 (🔴 BREAKPOINT 19) - COMMIT

---

## Step 3: Make a Test Request

Save this as `test_invoice.sh`:

```bash
#!/bin/bash

# Get JWT token
TOKEN=$(curl -s http://localhost:9000/token \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "00000000-0000-0000-0000-000000000003",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "entity_id": "00000000-0000-0000-0000-000000000002",
    "roles": ["invoice_creator"]
  }' | jq -r '.token')

# Create invoice (will hit breakpoints)
curl -X POST http://localhost:8000/invoices \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Idempotency-Key: debug-$(date +%s)" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "00000000-0000-0000-0000-000000000005",
    "po_reference": "PO-DEBUG-001",
    "invoice_date": "2026-07-21",
    "currency": "USD",
    "payment_terms": "NET30",
    "line_items": [
      {
        "description": "Widget",
        "quantity": 10,
        "unit_price": 100,
        "tax_rate": 18,
        "tax_jurisdiction": "IN"
      }
    ]
  }' | jq .
```

**Run it:**
```bash
chmod +x test_invoice.sh
./test_invoice.sh
```

---

## Step 4: VS Code Pauses

When you execute the curl request, VS Code will pause at the first breakpoint (line 230).

**You'll see:**
- Yellow highlight on the current line
- Variables panel on left showing local variables
- Call stack showing where you are

---

## Step 5: Inspect & Step

**Left Panel - Debug:**
- **Variables** → See `payload`, `current_user`, `db`, etc.
- **Watch** → Add expressions to track
- **Call Stack** → Show call hierarchy

**Keyboard Shortcuts:**
- **F10** → Step over (next line)
- **F11** → Step into (enter function)
- **F5** → Continue (jump to next breakpoint)
- **Shift+F11** → Step out

---

## Step 6: What to Look For at Each Breakpoint

### 🔴 BREAKPOINT 1 (Entry)
```
Variables to inspect:
  payload.customer_id = "00000000-0000-0000-0000-000000000005"
  payload.line_items = [LineItemCreate(...)]
  current_user.tenant_id = "00000000-0000-0000-0000-000000000001"
  x_idempotency_key = "debug-..."
```

### 🔴 BREAKPOINT 5 (Customer)
```
Variables to inspect:
  customer.id = "00000000-0000-0000-0000-000000000005"
  customer.name = "Acme Corp"
  customer.credit_limit = Decimal('100000')
```

### 🔴 BREAKPOINT 8 (Totals)
```
Variables to inspect:
  subtotal_total = Decimal('1000')
  tax_total = Decimal('180')
  grand_total = Decimal('1180')
  due_date = date(2026, 8, 20)
```

### 🔴 BREAKPOINT 12 (Credit)
```
Variables to inspect:
  outstanding = 0
  customer.credit_limit = Decimal('100000')
  base_grand_total = Decimal('14.16')
  Test: 0 + 14.16 ≤ 100000 ? ✅ YES
```

### 🔴 BREAKPOINT 14 (After Flush)
```
Variables to inspect:
  invoice.id = "inv-abc123"  ← NOW ASSIGNED!
  invoice.status = "DRAFT"
  invoice.version = 1
```

### 🔴 BREAKPOINT 19 (Before Commit)
```
All data ready to commit:
  invoice ✓
  line_items ✓
  idempotency_key ✓
  
After F5 → COMMIT happens
After that → All data in DB ✓
```

---

## Reading the Code Flow

The code follows this path:

```
REQUEST ARRIVES
    ↓
PARSE PAYLOAD (line 228)
    ↓
SETUP RLS (line 230)
    ↓
HASH PAYLOAD (line 247)
    ↓
CHECK IDEMPOTENCY (line 249)
    ├─ First call → Continue
    └─ Replay → Return cached
    ↓
LOOKUP CUSTOMER (line 263)
    ↓
GET BASE CURRENCY (line 286)
    ↓
CALCULATE TOTALS (line 293)
    ├─ qty × price = subtotal
    ├─ subtotal × tax_rate = tax
    └─ subtotal + tax = total
    ↓
RESOLVE FX RATE (line 310)
    └─ Convert to base currency
    ↓
CHECK CREDIT LIMIT (line 326)
    └─ outstanding + new ≤ limit ?
    ↓
CREATE INVOICE (line 343)
    └─ db.flush() → get ID
    ↓
CREATE LINE ITEMS (line 369)
    ↓
BUILD RESPONSE (line 408)
    ↓
COMPLETE IDEMPOTENCY (line 451)
    ↓
COMMIT (line 466)
    ↓
RETURN HTTP 201
```

---

## Database Inspection

While paused at a breakpoint, open another terminal:

```bash
# Check customer
docker compose exec -T db psql -U erp_user -d erp_db -c \
  "SELECT id, name, credit_limit FROM customer LIMIT 3;"

# Check what's in the invoice table
docker compose exec -T db psql -U erp_user -d erp_db -c \
  "SELECT id, status, total_amount, base_total_amount FROM invoice ORDER BY created_at DESC LIMIT 1;"

# Check idempotency keys
docker compose exec -T db psql -U erp_user -d erp_db -c \
  "SELECT key, endpoint, status FROM idempotency_key ORDER BY created_at DESC LIMIT 3;"
```

---

## Expected Numbers (for Reference)

When you run the test request with:
- qty: 10
- price: 100
- tax: 18%
- currency: USD
- base: USD (rate = 1.0)

You'll see:
```
Line total: 10 × 100 = 1000
Tax: 1000 × 0.18 = 180
Grand total: 1000 + 180 = 1180

Base total: 1180 × 1.0 = 1180 (USD to USD = no change)

Credit check: 0 + 1180 ≤ 100000 ✅ PASS
```

---

## Common Debugging Patterns

### Pattern 1: Check a Variable Value

At any breakpoint, look at the **Variables** panel (left side).

Example: At Breakpoint 8, you want `grand_total`:
```
Variables:
  subtotal_total: 1000
  tax_total: 180
  grand_total: 1180  ← HERE
```

### Pattern 2: Use Watch Expression

1. Click the **+** next to "WATCH"
2. Type: `customer.credit_limit > base_grand_total`
3. VS Code shows: `True` or `False`

### Pattern 3: Step Through Loop

At Breakpoint 7, you'll see a `for` loop.
- Press **F11** to step into the loop
- See each line_item processed
- Watch `subtotal_total` increment

### Pattern 4: Inspect Complex Objects

At Breakpoint 5, hover over `customer`:
- VS Code shows expandable tree
- Click ▶ to expand fields
- See all customer properties

---

## Replay Testing (Idempotency)

**Test idempotency by sending the same request twice:**

First request:
```bash
./test_invoice.sh
```
→ Creates invoice, returns HTTP 201

Second request (same key):
```bash
# Edit test_invoice.sh to use same X-Idempotency-Key, or:
curl ... -H "X-Idempotency-Key: debug-123" ...
```

→ At Breakpoint 3, `existing` will NOT be None
→ Returns cached response, HTTP 201
→ **Only ONE invoice created!** ✅

---

## Next Steps

Once you understand POST /invoices, explore:

1. **POST /invoices/{id}/approve** (line ~480)
   - Where GL journal entries are created
   - How status changes to "APPROVED"
   - Financial accounting logic

2. **POST /payments** 
   - Payment allocation (FIFO vs MANUAL)
   - How AR balance updates
   - Payment-to-invoice matching

3. **GET /customers/{id}/aging**
   - Materialized view query
   - AR aging report calculation

---

## Troubleshooting

**Q: Breakpoints not pausing?**
- A: Make sure VS Code debugger is attached
- A: Check that the request is hitting the Python code (not Envoy/gateway)

**Q: Variables showing `<unavailable>`?**
- A: Variable may not be defined yet
- A: Try stepping to next line

**Q: Can't see `invoice.id` at Breakpoint 13?**
- A: That's normal! It's not assigned until after `db.flush()` at Breakpoint 14

**Q: Want to see database state?**
- A: Pause at a breakpoint, open another terminal
- A: Query the DB with psql (see commands above)

---

## Summary

You have **20 strategic breakpoints** in `src/routers/invoices.py` that will let you:

✅ Watch request data flow through the system
✅ Inspect calculations at each step  
✅ Understand how idempotency works
✅ See how FX conversion happens
✅ Verify credit limits are checked
✅ Watch objects be created and flushed
✅ See the final response object
✅ Understand the COMMIT transaction

**You're ready to debug!** 🎉

Open VS Code, set breakpoints, and step through the code.
