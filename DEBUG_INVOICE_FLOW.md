# POST /invoices - Interactive Debugger Guide

## How to Use

Open **[src/routers/invoices.py](src/routers/invoices.py)** and look for the 🔴 **BREAKPOINT** comments.

You now have **20 strategic breakpoints** in the code. Here's how to step through:

### In VS Code:

1. **Open** `src/routers/invoices.py` (line 228)
2. **Click** on the line numbers where you see 🔴 to set breakpoints
3. **Make a test request**:
   ```bash
   curl -X POST http://localhost:8000/invoices \
     -H "X-Idempotency-Key: debug-001" \
     -H "Content-Type: application/json" \
     -d '{
       "customer_id": "00000000-0000-0000-0000-000000000005",
       "po_reference": "PO-001",
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
     }'
   ```
4. **Execution will pause** at each 🔴 breakpoint
5. **Inspect variables** in the VS Code Debug panel
6. **Press F10** to step to next line or **F5** to continue to next breakpoint

---

## Breakpoint Map

| # | Line | Name | What to Check |
|---|------|------|---------------|
| 1 | 228 | Entry Point | `payload`, `current_user`, `x_idempotency_key` |
| 2 | 247 | Hash Request | `request_hash` (SHA256 of payload) |
| 3 | 249 | Check Idempotency | `existing` (should be None for first call) |
| 4 | 270 | Customer Lookup | Customer query executing |
| 5 | 277 | Inspect Customer | `customer` object (id, name, credit_limit) |
| 6 | 286 | Get Base Currency | `base_currency` = "USD" |
| 7 | 293 | Calculate Line Totals | Loop through line items |
| 8 | 307 | Check Totals | `subtotal_total`, `tax_total`, `grand_total`, `due_date` |
| 9 | 310 | Resolve FX Rate | Query for exchange rate |
| 10 | 318 | Convert to Base | `base_subtotal_total`, `base_tax_total`, `base_grand_total` |
| 11 | 326 | Query Outstanding AR | Looking up previous invoices |
| 12 | 337 | Credit Limit Check | `outstanding` vs `customer.credit_limit` |
| 13 | 343 | Create Invoice Object | Invoice() constructed (not saved yet) |
| 14 | 368 | After Flush | `invoice.id` now assigned from DB |
| 15 | 369 | Create Line Items | InvoiceLineItem objects created |
| 16 | 385 | Fetch Saved Lines | `saved_line_items` retrieved |
| 17 | 408 | Build Response | JSON response object constructed |
| 18 | 451 | Complete Idempotency | Mark request as COMPLETED |
| 19 | 466 | COMMIT Transaction | All changes committed to DB |
| 20 | 470 | Return Response | HTTP 201 sent back to client |

---

## Key Variables to Watch

### Request Phase (Breakpoints 1-3)
- `payload.customer_id` → Which customer?
- `payload.line_items` → How many lines?
- `request_hash` → Unique fingerprint of payload
- `existing` → Is this a replay?

### Validation Phase (Breakpoints 4-6)
- `customer.credit_limit` → Credit available?
- `base_currency` → Entity's home currency
- `current_user.entity_id` → Which entity?

### Calculation Phase (Breakpoints 7-12)
- `subtotal_total` = 1000 (qty × price)
- `tax_total` = 180 (subtotal × tax_rate)
- `grand_total` = 1180 (subtotal + tax)
- `exchange_rate` → 1 USD = X foreign units
- `base_grand_total` → Amount in entity's currency
- `outstanding` → Sum of unpaid invoices

### Object Creation Phase (Breakpoints 13-16)
- `invoice.status` = "DRAFT" (not approved yet)
- `invoice.id` → Generated after flush
- `invoice.version` = 1 (for optimistic locking)
- `invoice.balance_amount` = `grand_total` (no payments yet)

### Response Phase (Breakpoints 17-20)
- `response_body` → JSON being sent
- After COMMIT → Data is permanent in DB
- HTTP 201 → Invoice created successfully

---

## Step-by-Step Walkthrough (TL;DR)

```
REQUEST (Breakpoint 1)
    ↓
HASH PAYLOAD (Breakpoint 2)
    ↓
CHECK IDEMPOTENCY (Breakpoint 3)
    ├─ If exists + complete → return cached response
    └─ If new → continue
    ↓
LOOKUP CUSTOMER (Breakpoints 4-5)
    ├─ RLS enforces tenant_id
    └─ Verify customer.is_active = True
    ↓
GET BASE CURRENCY (Breakpoint 6)
    ├─ Entity currency (e.g., USD)
    └─ Used for all GL postings
    ↓
CALCULATE LINE TOTALS (Breakpoints 7-8)
    ├─ subtotal = qty × unit_price
    ├─ tax = subtotal × tax_rate / 100
    └─ total = subtotal + tax
    ↓
RESOLVE FX RATE (Breakpoints 9-10)
    ├─ Query APPROVED rates
    ├─ Snapshot rate_id (immutable)
    └─ Convert all amounts to base currency
    ↓
CHECK CREDIT LIMIT (Breakpoints 11-12)
    ├─ Sum outstanding invoices
    └─ Verify (outstanding + new_invoice) ≤ credit_limit
    ↓
CREATE INVOICE (Breakpoints 13-14)
    ├─ status = "DRAFT" (not approved)
    ├─ balance_amount = total (no payments yet)
    ├─ db.flush() → invoice.id assigned
    └─ version = 1 (for updates)
    ↓
CREATE LINE ITEMS (Breakpoints 15-16)
    ├─ One row per line
    ├─ Linked to invoice.id
    └─ Stored for GL posting & credit allocation
    ↓
BUILD RESPONSE (Breakpoint 17)
    ├─ JSON serialization
    └─ Include all invoice fields
    ↓
MARK IDEMPOTENCY COMPLETE (Breakpoint 18)
    ├─ status = "COMPLETED"
    ├─ response_status = 201
    └─ response_body = <json>
    ↓
COMMIT (Breakpoint 19)
    ├─ All-or-nothing transaction
    ├─ Invoice + lines + idempotency key
    └─ Now permanent in DB
    ↓
RETURN HTTP 201 (Breakpoint 20)
    └─ Client receives response
```

---

## Testing Replay Behavior (Idempotency)

Send the **same request twice** with the **same X-Idempotency-Key**:

**First call:**
```bash
curl -X POST http://localhost:8000/invoices \
  -H "X-Idempotency-Key: my-key-123" \
  -H "Content-Type: application/json" \
  -d '{"customer_id": "...", ...}'
```
→ Hits Breakpoint 3, `existing` = None, proceeds to create

**Second call (identical):**
```bash
curl -X POST http://localhost:8000/invoices \
  -H "X-Idempotency-Key: my-key-123" \
  -H "Content-Type: application/json" \
  -d '{"customer_id": "...", ...}'
```
→ Hits Breakpoint 3, `existing` = <completed record>, returns cached response

**Result:** Only ONE invoice created, even though request sent twice ✅

---

## Testing Different Scenarios

### Scenario 1: New Invoice (Happy Path)
- First call → Creates invoice
- Second call with same key → Returns cached response

### Scenario 2: Customer Not Found
- Breakpoint 5: `customer` = None
- Raises HTTPException(404)

### Scenario 3: Credit Limit Exceeded
- Breakpoint 12: `outstanding` + `base_grand_total` > `credit_limit`
- Raises BusinessRuleException

### Scenario 4: FX Rate Not Available
- Breakpoint 9: No approved rate found
- Raises HTTPException(503)

---

## Inspect Database During Execution

While paused at a breakpoint, open another terminal and query the DB:

```bash
# See invoice that's being created
docker compose exec -T db psql -U erp_user -d erp_db -c \
  "SELECT id, status, total_amount FROM invoice ORDER BY created_at DESC LIMIT 1;"

# See line items
docker compose exec -T db psql -U erp_user -d erp_db -c \
  "SELECT line_number, description, quantity, unit_price FROM invoice_line_item LIMIT 5;"

# Check idempotency keys
docker compose exec -T db psql -U erp_user -d erp_db -c \
  "SELECT key, status, request_hash FROM idempotency_key ORDER BY created_at DESC LIMIT 3;"
```

---

## Next Steps

After understanding the POST /invoices flow, explore:

- **POST /invoices/{id}/approve** → Creates GL journal entries
- **POST /payments** → Allocates payment + updates AR aging
- **GET /customers/{id}/aging** → Queries materialized view
