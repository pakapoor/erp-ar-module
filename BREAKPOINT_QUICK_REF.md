# POST /invoices - QUICK REFERENCE CARD

## 20 Breakpoints at a Glance

| # | Line | Key Variable | What to Check |
|---|------|---|---|
| 1️⃣ | 230 | Entry | `payload`, `current_user`, `x_idempotency_key` |
| 2️⃣ | 247 | `request_hash` | SHA256 fingerprint of payload |
| 3️⃣ | 249 | `existing` | Is this a replay? (should be None) |
| 4️⃣ | 263 | Customer query | SQL executing with RLS |
| 5️⃣ | 277 | `customer` | id, name, credit_limit |
| 6️⃣ | 286 | `base_currency` | Entity home currency (USD, EUR) |
| 7️⃣ | 293 | Line loop | Iterating through line items |
| 8️⃣ | 307 | `grand_total` | subtotal + tax = total |
| 9️⃣ | 310 | FX query | Searching for approved rate |
| 🔟 | 318 | `base_grand_total` | Amount converted to base currency |
| 1️⃣1️⃣ | 326 | Outstanding query | Sum of unpaid invoices |
| 1️⃣2️⃣ | 337 | Credit check | outstanding + new_invoice ≤ limit? |
| 1️⃣3️⃣ | 343 | `invoice` object | Created in memory (not saved) |
| 1️⃣4️⃣ | 368 | After flush | `invoice.id` now assigned |
| 1️⃣5️⃣ | 369 | Line items | InvoiceLineItem objects created |
| 1️⃣6️⃣ | 385 | `saved_line_items` | Fetched from DB for response |
| 1️⃣7️⃣ | 408 | `response_body` | JSON dict built |
| 1️⃣8️⃣ | 451 | Idempotency | Mark as COMPLETED |
| 1️⃣9️⃣ | 466 | `db.commit()` | ALL-OR-NOTHING transaction |
| 2️⃣0️⃣ | 470 | HTTP 201 | Return response |

---

## Key Numbers to Remember

**Line Calculation Example:**
```
qty: 10
unit_price: 100
tax_rate: 18%

subtotal = 10 × 100 = 1000
tax = 1000 × 0.18 = 180
total = 1000 + 180 = 1180
```

**FX Example:**
```
currency: INR (transaction)
base: USD (entity)
rate: 0.012 (1 INR = 0.012 USD)

base_subtotal = 1000 × 0.012 = 12.00 USD
base_tax = 180 × 0.012 = 2.16 USD
base_total = 12.00 + 2.16 = 14.16 USD
```

**Credit Limit Check:**
```
outstanding = 0 (no previous invoices)
new_invoice = 14.16 USD
credit_limit = 100000 USD

Test: 0 + 14.16 ≤ 100000? ✅ YES
```

---

## Code Flow (TL;DR)

```
🔴1️⃣ Request arrives
    ↓
🔴2️⃣ Hash payload (idempotency key)
    ↓
🔴3️⃣ Check if replay? (return cached if yes)
    ↓
🔴4️⃣ Look up customer (with RLS)
🔴5️⃣ Inspect customer
🔴6️⃣ Get base currency
    ↓
🔴7️⃣ Calculate line totals (qty × price × tax)
🔴8️⃣ Sum all lines
    ↓
🔴9️⃣ Find FX rate (snapshot immutable)
🔴🔟 Convert to base currency
    ↓
🔴1️⃣1️⃣ Sum outstanding AR
🔴1️⃣2️⃣ Check credit limit
    ↓
🔴1️⃣3️⃣ Create Invoice object
🔴1️⃣4️⃣ db.flush() → get invoice.id
    ↓
🔴1️⃣5️⃣ Create line items
🔴1️⃣6️⃣ Fetch for response
    ↓
🔴1️⃣7️⃣ Build JSON response
    ↓
🔴1️⃣8️⃣ Mark idempotency COMPLETED
🔴1️⃣9️⃣ COMMIT (all-or-nothing)
    ↓
🔴2️⃣0️⃣ Return HTTP 201
```

---

## How to Debug

### Start Here: `src/routers/invoices.py` line 228

1. **Open file** in VS Code
2. **Look for** 🔴 symbols (20 of them)
3. **Click line number** to set breakpoint (red dot)
4. **Run request** in another terminal
5. **VS Code pauses** → inspect variables
6. **Press F10** to step next line or **F5** to continue

### Test Request (save as `test_invoice.sh`):

```bash
#!/bin/bash

TOKEN=$(curl -s http://localhost:9000/token \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "00000000-0000-0000-0000-000000000003",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "entity_id": "00000000-0000-0000-0000-000000000002",
    "roles": ["invoice_creator"]
  }' | jq -r '.token')

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
        "description": "Debug Widget",
        "quantity": 10,
        "unit_price": 100,
        "tax_rate": 18,
        "tax_jurisdiction": "IN"
      }
    ]
  }' | jq .
```

### Run:
```bash
chmod +x test_invoice.sh
./test_invoice.sh
```

---

## Debugging Tips

### Inspect Variable in Debug Console

```python
# In VS Code Debug Console:
customer.name
# → "Acme Corp"

customer.credit_limit
# → Decimal('100000')

invoice.id
# → "inv-abc123"

response_body["total_amount"]
# → "1180"
```

### Check Database While Paused

```bash
# In another terminal while paused:
docker compose exec -T db psql -U erp_user -d erp_db -c \
  "SELECT * FROM invoice ORDER BY created_at DESC LIMIT 1 \G"
```

### Watch Expression (Track Variable)

1. Right-click variable → "Add to Watch"
2. Value updates as you step

---

## Common Breakpoint Findings

| Breakpoint | What to Expect |
|---|---|
| 3 | `existing = None` (first call) |
| 5 | `customer.name = "Acme Corp"` |
| 6 | `base_currency = "USD"` |
| 8 | `grand_total = 1180` |
| 10 | `base_grand_total = 14.16` |
| 12 | `outstanding = 0`, credit OK ✅ |
| 14 | `invoice.id = "inv-..."` |
| 17 | `response_body["status"] = "DRAFT"` |
| 19 | After commit, data in DB ✅ |

---

## Next Steps

After mastering POST /invoices:
1. **POST /invoices/{id}/approve** → GL posting
2. **POST /payments** → Payment allocation
3. **GET /customers/{id}/aging** → AR aging report
