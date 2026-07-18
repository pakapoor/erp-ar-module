# API Design — ERP AR Module

## Table of Contents
- [Conventions](#conventions)
- [API1 — POST /invoices](#api1--post-invoices)
- [API2 — GET /invoices/{id}](#api2--get-invoicesid)
- [API3 — POST /invoices/{id}/approve](#api3--post-invoicesidapprove)
- [API4 — POST /payments](#api4--post-payments)
- [API5 — GET /customers/{id}/aging](#api5--get-customersidaging)
- [API6 — GET /journal-entries](#api6--get-journal-entries)

---

## Conventions

Base URL: /api/v1
Auth: Bearer JWT token
Tenant: Extracted from JWT (never from request body)
Content-Type: application/json

Common request headers:
Authorization: Bearer <jwt_token>
Content-Type: application/json
X-Idempotency-Key: <client-generated-uuid>  ← write operations only
If-Match: <version>                          ← concurrent write operations

Idempotency key rules:
- UUID generated ONCE per user action (not per retry)
- Same UUID on all retries of same action
- New user action = new UUID
- Never use timestamps in idempotency keys (midnight problem!)
- Expires 24 hours
- Stored in idempotency_keys table

Optimistic locking (ABA prevention):
- GET responses include "version" field
- Write operations include If-Match: <version> header
- Server rejects if version mismatch → 409 Conflict
- Forces client to re-fetch latest state before acting
- Version starts at 1 and increments on every invoice mutation, including
  status, balance, allocation, and credit memo changes
- Uses PostgreSQL version column — NOT Redis (Redis not durable enough for financial systems)

Common error response format:
{
  "error": {
    "code": "INVOICE_NOT_FOUND",
    "message": "Invoice 1001 not found",
    "details": {},
    "request_id": "abc-123"
  }
}

Common HTTP status codes:
200 → success (GET, state change)
201 → created (POST new resource)
202 → accepted (async bulk operations)
400 → bad request (validation)
401 → unauthorized (bad JWT)
403 → forbidden (wrong role or SOX violation)
404 → not found
409 → conflict (version mismatch / idempotency)
422 → unprocessable (business rule violation)
423 → locked (period closed/locked)
500 → server error

Synchronous vs Async:
- Single operations → synchronous (< 300ms p99)
- Bulk operations → async (202 Accepted + job_id)
- External side effects (email, PDF) → always async

Prototype lifecycle scope:
- The required prototype does not implement invoice delivery or a `/send` endpoint.
- Payment allocation accepts invoices in `APPROVED`, `SENT`, or
  `PARTIALLY_PAID` status. Approval establishes the receivable; delivery is not
  a prerequisite for recording money received.
- `SENT` remains in the domain lifecycle for a production delivery subsystem
  (email, EDI, e-invoicing, or customer portal), which would transition
  `APPROVED → SENT` and record delivery metadata.

---

## API1 — POST /invoices

Creates a new invoice in DRAFT status.

Request:
POST /api/v1/invoices
Authorization: Bearer <jwt>
X-Idempotency-Key: "550e8400-e29b-41d4-a716"

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

NOT in request body (server derives these):
- tenant_id    ← from JWT
- entity_id    ← from JWT
- status       ← always DRAFT
- total_amount ← calculated by server
- due_date     ← calculated: invoice_date + payment_terms (server owns all financial date calculations)

Response: HTTP 201 Created
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

Error cases:
400 → missing required fields
403 → user lacks invoice_creator role
404 → customer_id not found
409 → duplicate idempotency key
422 → customer exceeded credit limit
422 → invalid tax_jurisdiction

Future Enhancements (Phase 2):
- POST /invoices/bulk — async bulk invoice creation (202 Accepted + job_id)

---

## API2 — GET /invoices/{id}

Retrieves invoice with current balance, payment history, credit memo history, and status history.

Request:
GET /api/v1/invoices/uuid-1001
Authorization: Bearer <jwt>

Response: HTTP 200 OK
ETag: "5"  ← version number, used as If-Match on subsequent writes

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

No idempotency key needed — GET is read-only, no state changes.
ETag header = version number, used by client for subsequent write operations.

Error cases:
401 → invalid JWT
403 → invoice belongs to different entity
404 → invoice not found

---

## API3 — POST /invoices/{id}/approve

Approves invoice. Generates GL journal entry atomically.
Synchronous — single DB transaction, target < 300ms p99.

Request:
POST /api/v1/invoices/uuid-1001/approve
Authorization: Bearer <jwt>   ← Priya's token
X-Idempotency-Key: "550e8400-e29b-41d4-a716"
If-Match: "1"                 ← version from GET response

{
  "notes": "Approved after tax jurisdiction correction"
}

Atomic operations (all in ONE DB transaction):
1. Check idempotency key
2. Validate version (If-Match)
3. Validate invoice status = DRAFT
4. Check approver role (invoice_approver or cfo)
5. Check SOX segregation (approver != creator)
6. Check period is OPEN
7. Generate GL journal entry
8. Update invoice status → APPROVED
9. Increment version
10. Mark idempotency key COMPLETED

Response: HTTP 200 OK
{
  "id": "uuid-1001",
  "status": "APPROVED",
  "version": 2,
  "approved_by": "priya-uuid",
  "approved_at": "2024-01-15T10:00:00Z",
  "notes": "Approved after tax jurisdiction correction",
  "journal_entry_id": "uuid-JE001"
}

GL entries generated automatically:
Debit:  1200 Accounts Receivable  174000
Credit: 3100 Sales Revenue        174000

Error cases:
403 → user lacks invoice_approver role
403 → approver same as creator (SOX violation!)
      "Creator cannot approve their own invoice"
404 → invoice not found
409 → If-Match version mismatch (ABA problem!)
      "Invoice was modified. Please refresh."
422 → invoice not in DRAFT status
      "Cannot approve invoice in APPROVED status"
423 → period is CLOSED or LOCKED
      "January 2024 is locked. Cannot post."

Future Enhancements (Phase 2):
- POST /invoices/bulk-approve — async bulk approval (202 Accepted + job_id)
