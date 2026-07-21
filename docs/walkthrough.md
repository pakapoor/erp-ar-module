# Technical Walkthrough ERP AR Module

This document is your live guide during the technical session.
Open it on one side of your screen, run the demo on the other.

---

## Overview

This is a multi-tenant Accounts Receivable module built with FastAPI and PostgreSQL.

Six required APIs -- all implemented and tested:
1. POST /invoices
2. GET /invoices/{id}
3. POST /invoices/{id}/approve
4. POST /payments
5. GET /customers/{id}/aging
6. GET /journal-entries

Five bonus APIs -- implemented:
- PATCH /invoices/{id} -- edit a DRAFT invoice; also powers the reject-and-resubmit flow
- POST /invoices/{id}/credit-memos (FR-B1)
- POST /invoices/{id}/writeoff (FR-B2)
- POST /invoices/{id}/void (FR-B3)
- POST /invoices/{id}/approve now also accepts `action: REJECT` (reverts to DRAFT for correction)

200 checks passing. 70.9% code coverage.

Open: [requirements-traceability.md](requirements-traceability.md)

---

## System Architecture

Open: [high-level-design.md](high-level-design.md)

```
Client
  -> Envoy L7 Gateway (JWT check, rate limit, trace ID)
  -> FastAPI AR Application (Zero Trust JWT re-validation)
  -> PostgreSQL (single source of financial truth)
       |
  delivery_outbox -> SQS -> consumer -> adapter
```

Key decisions:
- Envoy is the only public entry point -- validates JWTs early
- AR Application re-validates JWT independently (Zero Trust -- gateway can be bypassed)
- No Redis -- PostgreSQL owns all financial state, locks, idempotency
- Modular monolith -- invoice + payment + GL share one ACID transaction
- Transactional outbox -- delivery committed with invoice, not dependent on SQS availability

---

## Data Model

Open: [data-model.md](data-model.md)

```
Tenant -> Entity -> Customer -> Invoice -> Line Items
                            -> Payment -> Allocation
                            -> Journal Entry -> Lines
```

- Every table has tenant_id -- RLS enforces isolation at DB level
- UUID primary keys on all tables
- Journal entries are immutable -- no UPDATE or DELETE ever
- Audit triggers fire at DB level -- cannot be bypassed by application code
- /health endpoint reconciles AR subledger to GL account 1200 continuously

---

## One Complete Financial Transaction

### Step 1 -- Create invoice (Rahul, invoice_creator)

```
POST /api/v1/invoices
X-Idempotency-Key: uuid

Server calculates (never trusts client):
  Subtotal:  INR 150,000
  Tax:       INR  24,000
  Total/AR:  INR 174,000
  Due date:  invoice_date + 30 days

Committed atomically:
  invoice (DRAFT, version 1)
  invoice_line_item x N
  idempotency_key (COMPLETED)
  audit_log (DB trigger)
```

### Step 2 -- Approve invoice (Priya, invoice_approver)

```
POST /api/v1/invoices/{id}/approve
If-Match: 1
X-Idempotency-Key: uuid

Controls checked:
  - Role: invoice_approver or cfo
  - SOX: approver_id != created_by
  - Status: must be DRAFT
  - Version: If-Match must match current version
  - Period: invoice_date period must be OPEN

GL entry generated:
  Dr 1200 Accounts Receivable  174,000
  Cr 3100 Sales Revenue        150,000
  Cr 2200 Tax Payable           24,000

All committed atomically:
  invoice (APPROVED, version 2)
  journal_entry + 3 journal_entry_lines
  delivery_outbox event
  idempotency_key (COMPLETED)
  audit_log (DB trigger)
```

### Step 2b -- Reject and correct (optional branch)

```
POST /api/v1/invoices/{id}/approve
{action: REJECT, rejection_reason: "..."}

  - Same role/SOX checks as approval (rejector != creator)
  - Invoice reverts to DRAFT, rejection_reason stored
  - NO GL entry, NO delivery

PATCH /api/v1/invoices/{id}
If-Match: <version>
{line_items: [...]}

  - invoice_creator only, DRAFT invoices only
  - Old line items replaced, totals recalculated, credit limit re-checked
  - rejection_reason cleared on save

POST /api/v1/invoices/{id}/approve   -- re-approve, now proceeds as Step 2
```

### Step 3 -- Delivery (async, non-blocking)

```
After commit:
  Publisher sends outbox event to SQS
  Consumer calls adapter -> records sent_at -> invoice -> SENT

SQS downtime cannot reverse the receivable or GL entry.
Three failed receives -> DLQ. CFO can requeue.
```

### Step 4 -- Record payment (Priya, payment_recorder)

```
POST /api/v1/payments
X-Idempotency-Key: uuid

SERIALIZABLE isolation (prevents double allocation)

AUTO mode: FIFO -- oldest due_date first
MANUAL mode: client specifies invoice + amount

GL entry (one for entire payment):
  Dr 1100 Cash                 100,000
  Cr 1200 Accounts Receivable  100,000

Invoice: PARTIALLY_PAID, balance INR 74,000

Defence in depth against duplicates:
  - Idempotency key + request hash
  - UNIQUE(tenant, customer, payment_reference)
  - SERIALIZABLE isolation
```

### Step 5 -- Verify

```
GET /invoices/{id}         -> shows payment history, balance INR 74,000
GET /customers/{id}/aging  -> INR 74,000 in current bucket (MV, 5 min stale)
GET /journal-entries       -> both journals, each balanced
GET /health                -> subledger AR = GL AR = INR 74,000  OK
```

---

## Five Strongest Tradeoffs

### 1. Modular monolith over microservices
Invoice + payment + GL share one ACID transaction. At 5-10 TPS normal load there is no scaling problem to solve. Decompose when a real boundary appears.

### 2. PostgreSQL only -- no Redis
Redis is not durable. Redis down = lock lost = double payment risk. PostgreSQL serializable isolation handles concurrent payment allocation safely. No external dependency for financial correctness.

### 3. Transactional outbox + SQS
Direct HTTP makes approval depend on notification. Direct SQS creates a dual-write gap. Outbox commits the event with the invoice atomically. SQS adds buffering and DLQ. Delivery failure never reverses accounting.

### 4. Live invoice detail, materialized aging
Invoice detail is live -- approver cannot act on stale balances. Aging is a portfolio view where 5-minute staleness is acceptable. Two strategies, explicit freshness timestamp on aging response.

### 5. Stored FX rates over live lookup
Worker fetches and validates rates. Financial APIs use approved stored rates, snapshotted per transaction. Reproducible journals, no provider dependency in payment latency. Max rate age: 24 hours before blocking.

---

## Likely Questions

**Why does approval succeed when SQS is down?**
Approval, GL and outbox commit in PostgreSQL. Notification is not financial truth. Publisher retries after SQS recovers.

**Why serializable for payments if invoices have versions?**
A payment allocates across multiple invoice rows simultaneously. A version protects one known row. Serializable protects the multi-row allocation decision.

**Why no Redis for idempotency?**
Idempotency key must commit in the same ACID transaction as the payment. Redis + DB = two systems = possible inconsistency on crash.

**Can RLS guarantee isolation by itself?**
Not completely in prototype -- runtime owner can bypass policies. Application predicates are tested. Production needs non-owner role + FORCE RLS.

**What would you build next?**
Non-owner forced RLS, immutable journal privileges, managed JWT/TLS rotation, DLQ alarms, PITR restore drills, load testing.

**Why can an approved invoice be paid before delivery?**
Approval establishes the receivable. Delivery is a communication side effect. Blocking payment on email failure would make operational state control financial truth.

**What happens on rejection -- does the approver lose the invoice?**
No. Rejection reverts the invoice to DRAFT with a stored reason. The creator edits it with PATCH (which clears the reason) and resubmits for approval -- no data is lost, no new invoice is created.

**What happens to overpayment?**
Cash debited for full amount, AR credited only for allocated amount, remainder credits Customer Credit liability (2100).

---

## Safe Live Demo Flow

```bash
# Start everything
docker compose up

# Seed test data
docker compose exec app python -m src.seed_data

# Run full test suite
./tests/integration/test_api.sh

# Open Swagger
http://localhost:8000/docs

# Health check
curl http://localhost:8000/health
```

---

## Honest Gaps (V2)

```
- Forced RLS (non-owner DB role)
- Credit memo notification
- Advance payment handling
- Revenue recognition / deferred revenue
- Intercompany elimination
- Historical aging (as_of parameter)
- Load testing
```
