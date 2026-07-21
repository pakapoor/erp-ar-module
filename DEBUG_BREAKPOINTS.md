# POST /invoices Debugger - Step-by-Step Walkthrough

## Open in VS Code

Open this file in VS Code:
```
src/routers/invoices.py
```

**Look for the 🔴 symbols** - these mark the 20 breakpoints.

---

## Breakpoint Locations (with line snippets)

### BREAKPOINT 1-3: Entry & Idempotency (Lines 228-260)

```python
228  @router.post("/invoices", status_code=status.HTTP_201_CREATED)
     async def create_invoice(
         payload: InvoiceCreate,
         x_idempotency_key: str = Header(...),
         current_user: CurrentUser = Depends(...),
         db: AsyncSession = Depends(get_db),
     ):
         # 🔴 BREAKPOINT 1: Entry point - request received
         await db.execute(...)  # Set RLS config
     
     # 🔴 BREAKPOINT 2: Hash the request payload
     request_hash = hashlib.sha256(...)
     
     # 🔴 BREAKPOINT 3: Check if this idempotency key already exists
     existing = await check_idempotency(...)
     if existing:
         return JSONResponse(...)  # Cached replay
```

**At Breakpoint 1, inspect:**
- `payload` → InvoiceCreate object
  - `customer_id`
  - `line_items` → List of items
  - `currency` → "USD", "INR", etc.
- `current_user` → Who's creating?
  - `tenant_id`, `entity_id`, `user_id`
  - `roles` → Should include "invoice_creator"
- `x_idempotency_key` → Request identifier

**At Breakpoint 2, note:**
- `request_hash` → SHA256(payload) → long hex string
- This uniquely identifies the request
- Used to prevent duplicate processing

**At Breakpoint 3, check:**
- `existing` → None for first call ✓
- `existing` → <record> for replay (return cached)

---

### BREAKPOINT 4-6: Customer & Entity (Lines 270-286)

```python
     # 🔴 BREAKPOINT 4: Look up customer (RLS enforced)
     result = await db.execute(
         select(Customer).where(
             and_(
                 Customer.id == payload.customer_id,
                 Customer.tenant_id == current_user.tenant_id,  # ← RLS
                 Customer.entity_id == current_user.entity_id,  # ← RLS
                 Customer.is_active == True,
             )
         )
     )
     # 🔴 BREAKPOINT 5: Inspect customer object
     customer = result.scalar_one_or_none()
     if not customer:
         raise HTTPException(status_code=404, ...)
     
     # 🔴 BREAKPOINT 6: Get entity's base currency
     base_currency = entity_currency_result.scalar_one()
```

**At Breakpoint 5, inspect:**
- `customer.id` → UUID
- `customer.name` → "Acme Corp"
- `customer.credit_limit` → 100000 (in base currency)
- `customer.currency` → "INR", "USD"

**At Breakpoint 6, note:**
- `base_currency` = "USD" (entity's home currency)
- All GL entries use this currency
- Invoices can be in different currency (FX conversion)

---

### BREAKPOINT 7-8: Line Item Calculation (Lines 293-307)

```python
     # 🔴 BREAKPOINT 7: Loop through line items and calculate totals
     for i, line in enumerate(payload.line_items, start=1):
         subtotal, tax_amount, total_price = calculate_line_totals(line)
         subtotal_total += subtotal
         tax_total += tax_amount
         line_items_data.append({
             "line_number": i,
             "subtotal": subtotal,
             "tax_amount": tax_amount,
             "total_price": total_price,
         })
     
     # 🔴 BREAKPOINT 8: Check calculated totals
     grand_total = subtotal_total + tax_total
     due_date = calculate_due_date(payload.invoice_date, payment_terms)
```

**Helper Function:**
```python
def calculate_line_totals(line):
    subtotal = line.quantity * line.unit_price
    tax_amount = subtotal * (line.tax_rate / 100)
    total_price = subtotal + tax_amount
    return subtotal, tax_amount, total_price
```

**At Breakpoint 7, step into loop:**
- Line 1: qty=10, price=100, tax=18%
  - subtotal = 10 × 100 = **1000**
  - tax = 1000 × 0.18 = **180**
  - total = 1000 + 180 = **1180**

**At Breakpoint 8, note:**
- `subtotal_total` = **1000**
- `tax_total` = **180**
- `grand_total` = **1180**
- `due_date` = invoice_date + 30 days

---

### BREAKPOINT 9-10: FX Rate Resolution (Lines 310-318)

```python
     # 🔴 BREAKPOINT 9: Resolve FX rate (immutable snapshot)
     exchange_rate_id, exchange_rate = await resolve_invoice_exchange_rate(
         db,
         current_user.tenant_id,
         payload.currency,        # "INR"
         base_currency,           # "USD"
         payload.invoice_date,
     )
     # 🔴 BREAKPOINT 10: Convert amounts to base currency
     base_subtotal_total = convert_to_base(subtotal_total, exchange_rate)
     base_tax_total = convert_to_base(tax_total, exchange_rate)
     base_grand_total = base_subtotal_total + base_tax_total
```

**At Breakpoint 9, inspect:**
- `exchange_rate_id` → UUID of rate snapshot
- `exchange_rate` → Decimal("0.012") = 1 INR → 0.012 USD
- Query checks: APPROVED + within 3 days of invoice_date

**At Breakpoint 10, note:**
- `base_subtotal_total` = 1000 × 0.012 = **12.00 USD**
- `base_tax_total` = 180 × 0.012 = **2.16 USD**
- `base_grand_total` = 12.00 + 2.16 = **14.16 USD**
- These are immutable (GL uses these values)

---

### BREAKPOINT 11-12: Credit Limit Check (Lines 326-337)

```python
     # 🔴 BREAKPOINT 11: Query outstanding invoices
     outstanding_result = await db.execute(
         select(text("COALESCE(SUM(base_balance_amount), 0)"))
         .select_from(Invoice).where(
             and_(
                 Invoice.customer_id == payload.customer_id,
                 Invoice.tenant_id == current_user.tenant_id,
                 Invoice.entity_id == current_user.entity_id,
                 Invoice.status.not_in(["PAID", "VOID", "WRITTEN_OFF"]),
             )
         )
     )
     # 🔴 BREAKPOINT 12: Check if credit limit would be exceeded
     outstanding = outstanding_result.scalar() or 0
     if customer.credit_limit > 0 and (outstanding + base_grand_total) > customer.credit_limit:
         raise BusinessRuleException("CREDIT_LIMIT_EXCEEDED", ...)
```

**At Breakpoint 11, note:**
- Querying sum of unpaid invoices (not PAID/VOID/WRITTEN_OFF)
- Uses `base_balance_amount` (entity currency)

**At Breakpoint 12, check:**
- `outstanding` = 0 (first invoice)
- `customer.credit_limit` = 100000 USD
- Test: 0 + 14.16 ≤ 100000 ✓ PASS

---

### BREAKPOINT 13-14: Create Invoice Record (Lines 343-368)

```python
     # 🔴 BREAKPOINT 13: Create Invoice object (in memory, not saved yet)
     invoice = Invoice(
         tenant_id=current_user.tenant_id,
         entity_id=current_user.entity_id,
         customer_id=payload.customer_id,
         po_reference=payload.po_reference,
         status="DRAFT",                    # ← ALWAYS starts as DRAFT
         transaction_currency=payload.currency,      # "INR"
         exchange_rate_id=exchange_rate_id,          # "fx-123"
         exchange_rate=exchange_rate,                # Decimal("0.012")
         base_currency=base_currency,                # "USD"
         subtotal_amount=subtotal_total,             # 1000 (INR)
         tax_amount=tax_total,                       # 180 (INR)
         total_amount=grand_total,                   # 1180 (INR)
         balance_amount=grand_total,                 # 1180 (INR) - no payments yet
         base_subtotal_amount=base_subtotal_total,   # 12.00 (USD)
         base_tax_amount=base_tax_total,             # 2.16 (USD)
         base_total_amount=base_grand_total,         # 14.16 (USD)
         base_balance_amount=base_grand_total,       # 14.16 (USD) - remaining
         invoice_date=payload.invoice_date,
         due_date=due_date,
         payment_terms=payload.payment_terms,        # "NET30"
         version=1,                                  # ← For optimistic locking
         created_by=current_user.user_id,
     )
     db.add(invoice)
     # 🔴 BREAKPOINT 14: After flush, invoice.id is assigned
     await db.flush()  # ← SQL INSERT executed
```

**At Breakpoint 13, inspect:**
- `invoice` object constructed (in memory)
- `status` = "DRAFT" (not approved, no GL entry yet)
- `version` = 1 (used for concurrency control)
- `balance_amount` = total (no payments deducted)
- Note: `invoice.id` is **NOT** assigned yet!

**At Breakpoint 14, note:**
- After `db.flush()`, SQLAlchemy sends INSERT to DB
- DB assigns `invoice.id` (UUID)
- Now `invoice.id` is available for line items

---

### BREAKPOINT 15-16: Create Line Items (Lines 369-385)

```python
     # 🔴 BREAKPOINT 15: Create line items (linked to invoice.id)
     for i, (line, totals) in enumerate(zip(payload.line_items, line_items_data)):
         line_item = InvoiceLineItem(
             tenant_id=current_user.tenant_id,
             invoice_id=invoice.id,              # ← Use ID from flush
             line_number=totals["line_number"],  # 1, 2, 3...
             description=line.description,       # "Widget"
             quantity=line.quantity,             # 10
             unit_price=line.unit_price,         # 100
             subtotal=totals["subtotal"],        # 1000
             tax_rate=line.tax_rate,             # 18
             tax_jurisdiction=line.tax_jurisdiction,  # "IN"
             tax_amount=totals["tax_amount"],    # 180
             total_price=totals["total_price"],  # 1180
         )
         db.add(line_item)
     
     await db.flush()
     
     # 🔴 BREAKPOINT 16: Fetch saved line items for response
     line_items_result = await db.execute(
         select(InvoiceLineItem)
         .where(InvoiceLineItem.invoice_id == invoice.id)
         .order_by(InvoiceLineItem.line_number)
     )
     saved_line_items = line_items_result.scalars().all()
```

**At Breakpoint 15, note:**
- Creating InvoiceLineItem object
- Uses `invoice.id` from previous flush ✓
- One row per line item

**At Breakpoint 16, inspect:**
- `saved_line_items` → List of InvoiceLineItem objects
- Each with id, line_number, description, quantity, unit_price, etc.

---

### BREAKPOINT 17: Build Response (Lines 408-448)

```python
     # 🔴 BREAKPOINT 17: Build JSON response object
     response_body = {
         "id": invoice.id,                  # UUID
         "status": invoice.status,          # "DRAFT"
         "version": invoice.version,        # 1
         "customer": {...},
         "invoice_date": str(...),
         "due_date": str(...),
         "currency": invoice.transaction_currency,   # "INR"
         "base_currency": invoice.base_currency,     # "USD"
         "exchange_rate": str(invoice.exchange_rate),  # "0.012"
         "total_amount": str(invoice.total_amount),  # "1180"
         "base_total_amount": str(invoice.base_total_amount),  # "14.16"
         "balance_amount": str(invoice.balance_amount),        # "1180"
         "base_balance_amount": str(invoice.base_balance_amount),  # "14.16"
         "line_items": [
             {
                 "line_number": 1,
                 "description": "Widget",
                 "quantity": "10",
                 "unit_price": "100",
                 "total_price": "1180",
             },
         ],
     }
```

**At Breakpoint 17, inspect:**
- `response_body` → Python dict
- Will be JSON-serialized
- Includes all invoice fields + line items

---

### BREAKPOINT 18-20: Idempotency & Commit (Lines 451-470)

```python
     # 🔴 BREAKPOINT 18: Mark request as COMPLETED in idempotency table
     await complete_idempotency_key(
         db,
         x_idempotency_key,
         current_user.tenant_id,
         current_user.entity_id,
         "POST /invoices",
         201,                    # HTTP status
         response_body,          # Cache response
     )
     # 🔴 BREAKPOINT 19: COMMIT all changes (invoice + lines + idempotency key)
     await db.commit()
     
     logger.info(f"Invoice {invoice.id} created by {current_user.user_id}")
     # 🔴 BREAKPOINT 20: Return HTTP 201 with response body
     return JSONResponse(status_code=201, content=response_body)
```

**At Breakpoint 18:**
- Updates idempotency_key table
- Sets status="COMPLETED"
- Caches response_body

**At Breakpoint 19:**
- COMMITS the entire transaction
- Invoice + line_items + idempotency_key all written
- ALL-OR-NOTHING: if error, entire txn rolls back

**At Breakpoint 20:**
- Returns HTTP 201 Created
- Client receives response_body as JSON

---

## How to Use the Debugger

### Step 1: Set Breakpoint in VS Code

- Open `src/routers/invoices.py`
- Click line number to set breakpoint (red dot appears)
- Or use keyboard: **Ctrl+K Ctrl+B** (or **Cmd+K Cmd+B** on Mac)

### Step 2: Make a Request

```bash
# In a separate terminal
curl -X POST http://localhost:8000/invoices \
  -H "Authorization: Bearer <JWT_TOKEN>" \
  -H "X-Idempotency-Key: debug-001" \
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
  }'
```

### Step 3: VS Code Pauses at Breakpoint

When you hit a breakpoint:
- Code execution pauses
- Yellow highlight shows current line
- Debug panel shows variables

### Step 4: Inspect Variables

In the **Debug Panel** (left side):
- **Variables** → Local variables with values
- **Watch** → Custom expressions
- **Call Stack** → Where you came from

### Step 5: Step Through Code

Use these keyboard shortcuts:
- **F10** → Step over (next line)
- **F11** → Step into (enter function)
- **Shift+F11** → Step out (exit function)
- **F5** → Continue (go to next breakpoint)

### Example Inspection at Breakpoint 8

```
Variables:
  subtotal_total: 1000
  tax_total: 180
  grand_total: 1180
  due_date: datetime.date(2026, 8, 20)
  payload:
    customer_id: "00000000-0000-0000-0000-000000000005"
    po_reference: "PO-DEBUG-001"
    invoice_date: date(2026, 7, 21)
    currency: "USD"
    payment_terms: "NET30"
    line_items:
      [0]:
        description: "Widget"
        quantity: Decimal("10")
        unit_price: Decimal("100")
        tax_rate: Decimal("18")
        tax_jurisdiction: "IN"
```

---

## Database Inspection During Debug

While paused, open another terminal:

```bash
# What customer was queried?
docker compose exec -T db psql -U erp_user -d erp_db -c \
  "SELECT id, name, credit_limit FROM customer WHERE id='00000000-0000-0000-0000-000000000005';"

# Invoice being created
docker compose exec -T db psql -U erp_user -d erp_db -c \
  "SELECT id, status, total_amount, base_total_amount FROM invoice ORDER BY created_at DESC LIMIT 1;"

# Idempotency key tracking
docker compose exec -T db psql -U erp_user -d erp_db -c \
  "SELECT key, endpoint, status FROM idempotency_key ORDER BY created_at DESC LIMIT 3;"
```

---

## Next: Debug POST /invoices/{id}/approve

Once you understand the creation flow, explore approval (GL posting):
- Where GL entries are created
- How invoice status changes to "APPROVED"
- Why balance_amount gets an "AR" entry
