# High-Level Design — ERP AR Module

## Table of Contents
- [Overview](#overview)
- [API1 + API2 + API3 — Invoice Lifecycle](#api1--api2--api3--invoice-lifecycle)
- [API4 — Payment Processing](#api4--payment-processing) ← coming soon
- [API5 — AR Aging Report](#api5--ar-aging-report) ← coming soon
- [API6 — Journal Entries](#api6--journal-entries) ← coming soon
- [NFR Layer](#nfr-layer) ← coming soon

---

## Overview

The system follows a modular monolith architecture for the prototype.
All components run in Docker on a single host.
PostgreSQL is the single source of truth — no Redis, no external cache.
Zero Trust security — JWT validated at both gateway and application layer.

Core request flow:
Client → L7 API Gateway → AR Application (FastAPI) → PostgreSQL

Key decisions:
- L7 Gateway: JWT signature check only — stateless, no header extraction
- AR Application: re-validates JWT independently (Zero Trust)
- No Redis: PostgreSQL enforces all consistency guarantees
- ACID transactions: all financial operations atomic
- Optimistic locking: version column prevents ABA problem
- Audit triggers: DB-level, cannot be bypassed by application code
- Period close: enforced at both app and DB trigger level

---

## API1 + API2 + API3 — Invoice Lifecycle

![Invoice lifecycle HLD](hld.svg)

### API1 — POST /invoices (FR1)
- Required role: invoice_creator
- X-Idempotency-Key required
- Server calculates: subtotal, tax, total, due_date (never trust client)
- ACID transaction: invoice + line_items saved atomically
- Result: 201 Created, status=DRAFT, version=1

### API2 — GET /invoices/{id} (FR1)
- Required role: any authenticated user
- No idempotency key (read only)
- Live JOIN across: invoice, line_item, payment, payment_allocation, credit_memo, audit_log
- ETag header = version number (used as If-Match on subsequent writes)
- No audit log written (reads not audited)
- Result: 200 + payment history + credit memo history + status history

### API3 — POST /invoices/{id}/approve (FR2, FR9, FR12, FR13)
- Required role: invoice_approver or cfo
- X-Idempotency-Key required
- If-Match: version required (ABA prevention)
- SOX check: approver_id must differ from created_by
- Period close check: invoice_date period must be OPEN
- GL entries generated atomically:
  - Debit  Accounts Receivable  total_amount
  - Credit Sales Revenue        subtotal_amount
  - Credit Tax Payable          tax_amount
- Result: 200, status=APPROVED, version+1, journal_entry_id returned

---

## API4 — Payment Processing
Coming soon

## API5 — AR Aging Report
Coming soon

## API6 — Journal Entries
Coming soon

## NFR Layer
Coming soon
