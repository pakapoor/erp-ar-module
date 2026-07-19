# API Design — ERP AR Module

## Table of Contents
- [Conventions](#conventions)
- [API1 — POST /invoices](#api1--post-invoices)
- [API2 — GET /invoices/{id}](#api2--get-invoicesid)
- [API3 — POST /invoices/{id}/approve](#api3--post-invoicesidapprove)
- [API4 — POST /payments](#api4--post-payments)
- [API5 — GET /customers/{id}/aging](#api5--get-customersidaging)
- [API6 — GET /journal-entries](#api6--get-journal-entries)
- [API7 — GET /health](#api7--get-health)
- [Future Enhancements](#future-enhancements)

---

## Conventions

**Base URL:** `/api/v1`
**Auth:** Bearer JWT token
**Tenant:** Extracted from JWT — never from request body
**Content-Type:** `application/json`

### Common Request Headers

```
Authorization:       Bearer <jwt_token>
Content-Type:        application/json
X-Idempotency-Key:   <client-generated-uuid>   ← write operations only
If-Match:            <version>                  ← concurrent write operations
```

### Idempotency Key Rules

```
- UUID generated ONCE per user action (not per retry)
- Same UUID on all retries of same action
- New user action = new UUID
- Never use timestamps (midnight problem!)
- Expires 24 hours
- Stored in the idempotency_key table
- Returned in POST responses for audit trail
- Scoped by tenant_id + entity_id + endpoint + key
- SHA-256 request hash detects reuse with a different payload

Flow:
User clicks button → client generates UUID → stores in memory
Network timeout   → client retries with SAME UUID
Server sees key   → already COMPLETED → returns cached response
No duplicate processing!

Server outcomes:
→ COMPLETED + same hash  → replay original HTTP status and response body
→ PROCESSING + same hash → 409 REQUEST_IN_PROGRESS
→ same key + new hash    → 409 IDEMPOTENCY_KEY_REUSED
```

### Optimistic Locking — ABA Prevention

```
Problem: Two users act on same invoice simultaneously
         OR same user has stale browser tab

Solution: Version-based optimistic locking

GET /invoices/1001 → response includes "version": 1
                     response header: ETag: "1"

POST /invoices/1001/approve
If-Match: "1"   ← must match current version

Server:
UPDATE invoice SET status='APPROVED', version=version+1
WHERE id='1001' AND version=1   ← exact version check

0 rows updated → 409 Conflict → "Please refresh"
1 row updated  → success ✅

Why NOT Redis locks:
→ Redis is in-memory, not durable
→ Redis down = lock lost = double processing
→ Financial systems need DB-level locking only
→ PostgreSQL version column = ACID guaranteed
```

The version starts at 1 and increments on every invoice mutation, including
status, balance, payment allocation, and credit memo changes.

### Common Error Response Format

```json
{
  "error": {
    "code": "INVOICE_NOT_FOUND",
    "message": "Invoice uuid-1001 not found",
    "details": {},
    "request_id": "abc-123"
  }
}
```

### HTTP Status Codes

```
200 → success (GET, state change)
201 → created (POST new resource)
202 → accepted (async bulk operations)
400 → bad request (validation failed)
401 → unauthorized (invalid/expired JWT)
403 → forbidden (wrong role or SOX violation)
404 → not found
409 → conflict (version mismatch / request in progress / idempotency key misuse)
422 → unprocessable (business rule violation)
423 → locked (accounting period closed/locked)
500 → server error
```

### Synchronous vs Async

```
Single operations  → synchronous (< 300ms p99)
Bulk operations    → async (202 Accepted + job_id)
External effects   → always async (email, PDF, webhooks)

Polling async jobs:
GET /jobs/{job_id}
{
  "status": "processing|completed|failed",
  "processed": 998,
  "failed": 2,
  "errors": [...]
}
```

---

## API1 — POST /invoices

Creates a new invoice in DRAFT status.

**Required role:** `invoice_creator`

### Request

```
POST /api/v1/invoices
Authorization: Bearer <jwt>
X-Idempotency-Key: "550e8400-e29b-41d4-a716"
Content-Type: application/json
```

```json
{
  "customer_id": "uuid-tata-steel",
  "po_reference": "PO-2024-789",
  "invoice_date": "2024-01-15",
  "payment_terms": "NET30",
  "currency": "INR",
  "line_items": [
    {
      "description": "Industrial Pump",
      "quantity": 2,
      "unit_price": 50000,
      "tax_rate": 18,
      "tax_jurisdiction": "MH"
    },
    {
      "description": "Safety Valves",
      "quantity": 10,
      "unit_price": 5000,
      "tax_rate": 12,
      "tax_jurisdiction": "KA"
    }
  ]
}
```

**NOT in request body — server derives these:**
```
tenant_id    ← from JWT (never trust client)
entity_id    ← from JWT (never trust client)
status       ← always DRAFT on creation
total_amount ← calculated by server (sum of line items)
due_date     ← calculated: invoice_date + payment_terms
               server owns all financial date calculations
               client cannot manipulate due dates
```

### Response — HTTP 201 Created

```json
{
  "id": "uuid-1001",
  "status": "DRAFT",
  "version": 1,
  "customer": {
    "id": "uuid-tata-steel",
    "name": "Tata Steel"
  },
  "invoice_date": "2024-01-15",
  "due_date": "2024-02-14",
  "payment_terms": "NET30",
  "currency": "INR",
  "line_items": [
    {
      "id": "uuid-line-1",
      "line_number": 1,
      "description": "Industrial Pump",
      "quantity": 2,
      "unit_price": 50000,
      "subtotal": 100000,
      "tax_rate": 18,
      "tax_jurisdiction": "MH",
      "tax_amount": 18000,
      "total_price": 118000
    },
    {
      "id": "uuid-line-2",
      "line_number": 2,
      "description": "Safety Valves",
      "quantity": 10,
      "unit_price": 5000,
      "subtotal": 50000,
      "tax_rate": 12,
      "tax_jurisdiction": "KA",
      "tax_amount": 6000,
      "total_price": 56000
    }
  ],
  "subtotal_amount": 150000,
  "tax_amount": 24000,
  "total_amount": 174000,
  "balance_amount": 174000,
  "created_by": "rahul-uuid",
  "created_at": "2024-01-15T09:00:00Z"
}
```

### Error Cases

```
400 → missing required fields (customer_id, line_items)
403 → user lacks invoice_creator role
404 → customer_id not found
409 → REQUEST_IN_PROGRESS (same request is still processing)
409 → IDEMPOTENCY_KEY_REUSED (same key, different request payload)
422 → customer exceeded credit limit
422 → invalid tax_jurisdiction
422 → line item quantity or price <= 0
```

---

## API2 — GET /invoices/{id}

Retrieves invoice with current balance, payment history,
credit memo history, and status history (SOX requirement).

**Required role:** Any authenticated user of same entity

### Request

```
GET /api/v1/invoices/uuid-1001
Authorization: Bearer <jwt>
```

No idempotency key needed — GET is read-only, no state changes.

### Response — HTTP 200 OK

**Response Headers:**
```
ETag: "5"   ← version number, used as If-Match on subsequent writes
```

```json
{
  "id": "uuid-1001",
  "status": "PARTIALLY_PAID",
  "version": 5,
  "customer": {
    "id": "uuid-tata-steel",
    "name": "Tata Steel"
  },
  "invoice_date": "2024-01-15",
  "due_date": "2024-02-14",
  "payment_terms": "NET30",
  "currency": "INR",
  "line_items": [
    {
      "id": "uuid-line-1",
      "line_number": 1,
      "description": "Industrial Pump",
      "quantity": 2,
      "unit_price": 50000,
      "subtotal": 100000,
      "tax_rate": 18,
      "tax_jurisdiction": "MH",
      "tax_amount": 18000,
      "total_price": 118000
    },
    {
      "id": "uuid-line-2",
      "line_number": 2,
      "description": "Safety Valves",
      "quantity": 10,
      "unit_price": 5000,
      "subtotal": 50000,
      "tax_rate": 12,
      "tax_jurisdiction": "KA",
      "tax_amount": 6000,
      "total_price": 56000
    }
  ],
  "subtotal_amount": 150000,
  "tax_amount": 24000,
  "total_amount": 174000,
  "balance_amount": 64000,
  "payment_history": [
    {
      "payment_id": "uuid-P001",
      "payment_reference": "HDFC2024012000123",
      "payment_date": "2024-01-20",
      "payment_method": "RTGS",
      "amount_allocated": 100000,
      "currency": "INR"
    }
  ],
  "credit_memo_history": [
    {
      "credit_memo_id": "uuid-CM001",
      "reason_code": "RETURN",
      "amount": 10000,
      "applied_at": "2024-01-25T09:00:00Z"
    }
  ],
  "status_history": [
    {
      "status": "DRAFT",
      "changed_by": "Rahul",
      "changed_at": "2024-01-15T09:00:00Z"
    },
    {
      "status": "APPROVED",
      "changed_by": "Priya",
      "changed_at": "2024-01-15T10:00:00Z"
    },
    {
      "status": "SENT",
      "changed_by": "Rahul",
      "changed_at": "2024-01-15T11:00:00Z"
    },
    {
      "status": "PARTIALLY_PAID",
      "changed_by": "System",
      "changed_at": "2024-01-20T14:00:00Z"
    }
  ],
  "created_by": "rahul-uuid",
  "approved_by": "priya-uuid",
  "created_at": "2024-01-15T09:00:00Z",
  "approved_at": "2024-01-15T10:00:00Z"
}
```

### Error Cases

```
401 → invalid/expired JWT
403 → invoice belongs to different entity
404 → invoice not found
```

---

## API3 — POST /invoices/{id}/approve

Approves invoice. Generates GL journal entry atomically.
Synchronous — single DB transaction, target < 300ms p99.

**Required role:** `invoice_approver` or `cfo`
**SOX requirement:** approver must differ from creator

### Request

```
POST /api/v1/invoices/uuid-1001/approve
Authorization: Bearer <jwt>   ← Priya's token
X-Idempotency-Key: "550e8400-e29b-41d4-a716"
If-Match: "1"                 ← version from GET response
Content-Type: application/json
```

```json
{
  "notes": "Approved after tax jurisdiction correction"
}
```

### Atomic Operations (ONE DB transaction)

```
1.  Check idempotency key → already COMPLETED? return cached response
2.  Validate If-Match version → mismatch? 409 Conflict
3.  Validate invoice status = DRAFT
4.  Check approver role (invoice_approver or cfo)
5.  Check SOX: approver_id != created_by
6.  Find the tenant + entity period containing invoice_date; require OPEN
7.  Generate GL journal entry:
    Debit:  1200 AR           174000
    Credit: 3100 Revenue      150000
    Credit: 2200 Tax Payable   24000
8.  Update invoice status → APPROVED
9.  Increment version (1 → 2)
10. Insert PENDING invoice-delivery outbox event
11. Mark idempotency key COMPLETED
12. Write audit log
```

### Response — HTTP 200 OK

```json
{
  "id": "uuid-1001",
  "status": "APPROVED",
  "version": 2,
  "approved_by": "priya-uuid",
  "approved_at": "2024-01-15T10:00:00Z",
  "notes": "Approved after tax jurisdiction correction",
  "journal_entry_id": "uuid-JE001",
  "delivery_status": "QUEUED"
}
```

Minimal response — client already has full invoice from GET.
Full details available via GET /invoices/{id} with new version.

After commit, a separate worker claims the event using `FOR UPDATE SKIP LOCKED`
and calls the delivery stub with the outbox event ID as an idempotency key.
Failures retry with backoff without rolling back approval. Successful delivery
records `sent_at` and transitions APPROVED to SENT; a later payment status is
never overwritten.

### Error Cases

```
403 → user lacks invoice_approver role
403 → approver same as creator (SOX violation!)
      "Creator cannot approve their own invoice"
404 → invoice not found
409 → REQUEST_IN_PROGRESS (same request is still processing)
409 → IDEMPOTENCY_KEY_REUSED (same key, different request payload)
409 → If-Match version mismatch (ABA problem!)
      "Invoice was modified since last viewed. Please refresh."
422 → invoice not in DRAFT status
      "Cannot approve invoice with status: APPROVED"
423 → accounting period CLOSED or LOCKED
      CLOSED: CFO may reopen with a mandatory audited reason, then retry
      LOCKED: never reopen; use a CFO-approved current-period adjustment
```

`invoice_date` is the business document date. For normal approval, the journal
entry posts to that date's OPEN period. A prior-period adjustment preserves the
original document date but uses an `entry_date` in the current OPEN period; the
system never silently shifts or backdates an entry.

### Future Enhancements (Phase 2)

```
POST /api/v1/invoices/bulk-approve
→ async bulk approval
→ 202 Accepted + job_id
→ client polls GET /jobs/{job_id}
```

---

## API4 — POST /payments

Records a payment and allocates to one or more invoices.
Supports AUTO (FIFO) and MANUAL allocation modes.

**Required role:** `payment_recorder`

### Request — AUTO Mode (FIFO)

```
POST /api/v1/payments
Authorization: Bearer <jwt>
X-Idempotency-Key: "550e8400-e29b-41d4-a716"
Content-Type: application/json
```

```json
{
  "customer_id": "uuid-tata-steel",
  "payment_reference": "HDFC2024013100123",
  "payment_date": "2024-01-31",
  "amount": 300000,
  "currency": "INR",
  "payment_method": "RTGS",
  "allocation_mode": "AUTO",
  "allocations": null
}
```

### Request — MANUAL Mode

```json
{
  "customer_id": "uuid-tata-steel",
  "payment_reference": "HDFC2024013100123",
  "payment_date": "2024-01-31",
  "amount": 200000,
  "currency": "INR",
  "payment_method": "RTGS",
  "allocation_mode": "MANUAL",
  "allocations": [
    {"invoice_id": "uuid-1001", "amount": 100000},
    {"invoice_id": "uuid-1002", "amount": 60000},
    {"invoice_id": "uuid-1003", "amount": 40000}
  ]
}
```

### Validations

Approval establishes the receivable, so both allocation modes accept invoices
in `APPROVED`, `SENT`, or `PARTIALLY_PAID` status. This also permits payment
while asynchronous delivery is pending or being retried.

```
Customer:
→ customer_id exists and belongs to tenant
→ customer is active

Invoices (AUTO mode):
→ customer has open invoices
→ invoices in APPROVED/SENT/PARTIALLY_PAID status only
→ not DRAFT/VOID/WRITTEN_OFF

Invoices (MANUAL mode):
→ each invoice_id exists and belongs to customer
→ each invoice is in APPROVED/SENT/PARTIALLY_PAID status
→ sum of allocations <= payment amount
→ each allocation <= invoice outstanding balance

Amount:
→ amount > 0
→ no allocation exceeds invoice balance

Currency:
→ exchange rate exists for payment_date
→ if missing → use most recent available rate + flag in response

Duplicate prevention:
→ COMPLETED key + same request hash replays cached 201 response
→ PROCESSING key + same request hash returns 409 REQUEST_IN_PROGRESS
→ same key + different request hash returns 409 IDEMPOTENCY_KEY_REUSED
→ payment_reference unique within tenant + customer
   (prevents double RTGS processing)
```

### Atomic Operations (ONE DB transaction)

```
1. Claim unique (tenant_id, entity_id, endpoint, idempotency_key) with request_hash
2. Create payment and allocate invoices under serializable isolation
3. Generate balanced GL journal entry
4. Update invoice balances, statuses, and versions
5. Store original HTTP status + response; mark key COMPLETED
6. Commit everything together

Crash before commit → all changes, including PROCESSING key, roll back
Crash after commit  → retry replays cached response
```

### Response — HTTP 201 Created

```json
{
  "id": "uuid-P001",
  "idempotency_key": "550e8400-e29b-41d4-a716",
  "status": "APPLIED",
  "customer_id": "uuid-tata-steel",
  "payment_reference": "HDFC2024013100123",
  "payment_date": "2024-01-31",
  "amount": 300000,
  "currency": "INR",
  "payment_method": "RTGS",
  "allocation_mode": "AUTO",
  "allocations": [
    {
      "invoice_id": "uuid-1001",
      "amount_allocated": 174000,
      "invoice_balance_before": 174000,
      "invoice_balance_after": 0,
      "invoice_status": "PAID"
    },
    {
      "invoice_id": "uuid-1002",
      "amount_allocated": 80000,
      "invoice_balance_before": 80000,
      "invoice_balance_after": 0,
      "invoice_status": "PAID"
    },
    {
      "invoice_id": "uuid-1003",
      "amount_allocated": 46000,
      "invoice_balance_before": 46000,
      "invoice_balance_after": 0,
      "invoice_status": "PAID"
    }
  ],
  "allocated_amount": 300000,
  "unallocated_amount": 0,
  "overpayment_amount": 0,
  "overpayment_action": null,
  "exchange_rate_used": 1.0,
  "exchange_rate_date": "2024-01-31",
  "exchange_rate_warning": null,
  "journal_entry_id": "uuid-JE002",
  "created_at": "2024-01-31T14:00:00Z"
}

Overpayment example (payment > total outstanding):
{
  "allocated_amount": 300000,
  "unallocated_amount": 100000,
  "overpayment_amount": 100000,
  "overpayment_action": "ON_ACCOUNT",  ← REFUND/ON_ACCOUNT/ADVANCE
  ...
}

Exchange rate warning example (rate missing for payment date):
{
  "exchange_rate_used": 83.00,
  "exchange_rate_date": "2024-01-30",
  "exchange_rate_warning": "Rate from 2024-01-30 used (2024-01-31 not available)",
  ...
}
```

**Idempotency key in response** — for audit trail and debugging.
Support team can trace any payment dispute to exact request.

**GL entries generated automatically:**
```
Debit:  1100 Cash    300000  ← money arrived
Credit: 1200 AR      300000  ← debt cleared
```

For an INR 400,000 receipt against INR 300,000 outstanding:

```text
Debit:  1100 Cash             400000
Credit: 1200 AR               300000
Credit: 2100 Customer Credit  100000  ← unapplied liability
```

**FX payment GL entries (if currency differs):**
```
Debit:  1100 Cash          86000  ← actual cash (at payment rate)
Credit: 1200 AR            83000  ← original invoice amount (at invoice rate)
Credit: 4300 FX Gain/Loss   3000  ← difference
```

### Error Cases

```
400 → missing required fields
403 → user lacks payment_recorder role
404 → customer_id not found
404 → invoice_id not found (manual mode)
409 → REQUEST_IN_PROGRESS (same request is still processing)
409 → IDEMPOTENCY_KEY_REUSED (same key, different request payload)
409 → duplicate payment_reference for same customer
422 → amount <= 0
422 → allocation exceeds invoice outstanding balance
422 → sum of manual allocations > payment amount
422 → invoice not in payable status (DRAFT/VOID/WRITTEN_OFF)
422 → no open invoices found (auto mode)
503 → no current or prior exchange rate is available
```

### Future Enhancements (Phase 2)

```
POST /api/v1/payments/bulk
→ async bulk payment import
→ 202 Accepted + job_id
→ client polls GET /jobs/{job_id}

POST /api/v1/payments/webhook
→ payment gateway webhook handler
→ Stripe/Razorpay payment confirmation
```

---

## API5 — GET /customers/{id}/aging

Returns AR aging summary for a customer.
Reads from materialized view — refreshed every 5 minutes.
Shows explicit as_of timestamp so user knows data freshness.

**Required role:** Any authenticated user of same entity

### Request

```
GET /api/v1/customers/uuid-tata-steel/aging
    ?entity_id=uuid-retail
Authorization: Bearer <jwt>
```

**Query Parameters:**
```
entity_id  ← optional, defaults to JWT entity
```

The prototype returns current aging only. The response `as_of` value is the
materialized-view refresh timestamp, not a caller-selected historical date.

No idempotency key or ETag needed:
```
→ GET is read only
→ Aging data is informational
→ Client acts on individual invoices (each has own version)
→ Not on the aging summary itself
```

### Response — HTTP 200 OK

```json
{
  "customer": {
    "id": "uuid-tata-steel",
    "name": "Tata Steel"
  },
  "entity_id": "uuid-retail",
  "as_of": "2024-01-31T14:35:00Z",
  "data_freshness": "5 minutes",
  "currency": "INR",
  "buckets": {
    "current": {
      "amount": 80000,
      "invoice_count": 1,
      "invoices": ["uuid-1003"]
    },
    "days_30": {
      "amount": 74000,
      "invoice_count": 1,
      "invoices": ["uuid-1002"]
    },
    "days_60": {
      "amount": 0,
      "invoice_count": 0,
      "invoices": []
    },
    "days_90_plus": {
      "amount": 0,
      "invoice_count": 0,
      "invoices": []
    }
  },
  "total_outstanding": 154000
}
```

### Error Cases

```
403 → customer belongs to different tenant
404 → customer not found
```

### Future Enhancement — Historical Aging

A historical `as_of` query must reconstruct the balance from dated payments,
allocations, credit memos, write-offs, voids, and reversals, or read from
persisted daily snapshots. It cannot use the invoice's current
`balance_amount`. This is outside the required prototype scope.

---

## API6 — GET /journal-entries

Retrieves GL journal entries for an invoice.
invoice_id is required. Other filters optional.
Supports pagination for invoices with many entries.

**Required role:** Any authenticated user of the same entity. Cross-entity
auditor access requires an explicit entity-membership model and is deferred;
the prototype never treats a role claim alone as permission to cross entities.

### Request

```
GET /api/v1/journal-entries
    ?invoice=uuid-1001
    &page=1
    &page_size=20
Authorization: Bearer <jwt>
```

**Query Parameters:**
```
invoice      ← REQUIRED primary filter (invoice ID)
payment_id   ← optional additional filter
from_date    ← optional date range start
to_date      ← optional date range end
account_code ← optional GL account filter
page         ← default 1
page_size    ← default 20, max 100
```

### Response — HTTP 200 OK

```json
{
  "invoice_id": "uuid-1001",
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total": 3,
    "total_pages": 1
  },
  "journal_entries": [
    {
      "id": "uuid-JE001",
      "reference_type": "INVOICE",
      "reference_id": "uuid-1001",
      "document_date": "2024-01-15",
      "entry_date": "2024-01-15",
      "description": "Invoice #1001 approved",
      "created_by": "priya-uuid",
      "lines": [
        {
          "account_code": "1200",
          "account_name": "Accounts Receivable",
          "debit_amount": 174000,
          "credit_amount": 0
        },
        {
          "account_code": "3100",
          "account_name": "Sales Revenue",
          "debit_amount": 0,
          "credit_amount": 150000
        },
        {
          "account_code": "2200",
          "account_name": "Tax Payable",
          "debit_amount": 0,
          "credit_amount": 24000
        }
      ],
      "total_debits": 174000,
      "total_credits": 174000,
      "balanced": true
    },
    {
      "id": "uuid-JE002",
      "reference_type": "PAYMENT",
      "reference_id": "uuid-P001",
      "document_date": "2024-01-20",
      "entry_date": "2024-01-20",
      "description": "Payment P001 received from Tata Steel",
      "created_by": "system",
      "lines": [
        {
          "account_code": "1100",
          "account_name": "Cash",
          "debit_amount": 100000,
          "credit_amount": 0
        },
        {
          "account_code": "1200",
          "account_name": "Accounts Receivable",
          "debit_amount": 0,
          "credit_amount": 100000
        }
      ],
      "total_debits": 100000,
      "total_credits": 100000,
      "balanced": true
    },
    {
      "id": "uuid-JE003",
      "reference_type": "CREDIT_MEMO",
      "reference_id": "uuid-CM001",
      "document_date": "2024-01-25",
      "entry_date": "2024-01-25",
      "description": "Credit memo CM001 — goods returned",
      "created_by": "priya-uuid",
      "lines": [
        {
          "account_code": "3100",
          "account_name": "Sales Revenue",
          "debit_amount": 10000,
          "credit_amount": 0
        },
        {
          "account_code": "1200",
          "account_name": "Accounts Receivable",
          "debit_amount": 0,
          "credit_amount": 10000
        }
      ],
      "total_debits": 10000,
      "total_credits": 10000,
      "balanced": true
    }
  ],
  "summary": {
    "total_debited_ar": 174000,
    "total_credited_ar": 110000,
    "net_ar_balance": 64000
  }
}
```

**`balanced: true`** on every entry — debits always equal credits.
If ever `false` → system RED alert, immediate investigation!

Pagination applies only to `journal_entries`; `summary` always represents all
journal entries for the invoice. Requests beyond `total_pages` return HTTP 200
with an empty list, allowing clients to handle concurrent page navigation
without treating it as a missing resource.

### Error Cases

```
400 → invoice_id not provided (required filter)
403 → invoice belongs to different tenant
404 → invoice not found
```

---

## API7 — GET /health

Health check endpoint for Docker and load balancer.

### Request

```
GET /health
```

No auth required — used by infrastructure.

### Response — HTTP 200 OK

```json
{
  "status": "healthy",
  "version": "1.0.0",
  "timestamp": "2024-01-15T10:00:00Z",
  "checks": {
    "database": "healthy",
    "materialized_view_age_minutes": 3,
    "last_reconciliation": "2024-01-15T09:55:00Z",
    "reconciliation_status": "MATCHED"
  }
}
```

### Response — HTTP 503 Service Unavailable

```json
{
  "status": "unhealthy",
  "checks": {
    "database": "unhealthy",
    "error": "Connection timeout"
  }
}
```

---

## Future Enhancements

### Additional Write APIs (Phase 2)

```
POST /invoices/{id}/send          ← FR3: send invoice to customer
POST /invoices/{id}/void          ← FR7: void invoice
POST /invoices/{id}/credit-memos  ← FR5: create credit memo
POST /invoices/{id}/writeoff      ← FR6: write off invoice
POST /journal-entries/manual      ← FR15: manual journal entry
POST /periods/{id}/close          ← FR13: close accounting period
POST /periods/{id}/reopen         ← FR13: CFO reopens CLOSED period with reason
POST /periods/{id}/lock           ← FR13: lock accounting period
POST /users                       ← FR16: create user
POST /users/{id}/roles            ← FR16: assign role
```

### Bulk Operations (Phase 2)

```
POST /invoices/bulk-approve       ← async, 202 Accepted
POST /invoices/bulk               ← async, 202 Accepted
POST /payments/bulk               ← async, 202 Accepted
GET  /jobs/{job_id}               ← poll async job status
```

### Reporting APIs (Phase 2)

```
GET /reports/consolidated         ← FR14: consolidated multi-entity report
GET /reports/reconciliation       ← FR9: AR vs GL reconciliation status
GET /audit-log                    ← FR12: SOX audit trail
```
