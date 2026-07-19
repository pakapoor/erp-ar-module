# Principal Engineer ERP Assessment — Consolidated Submission

## 1. Scope and Design Position

This prototype implements the six APIs required by the assessment and demonstrates one complete financial path: create an invoice, retrieve it, approve it, generate the receivable GL entry, apply a partial payment, report aging, and retrieve the resulting journal trail.

The architecture is a modular monolith implemented with FastAPI and PostgreSQL. This keeps invoice, payment, allocation, audit, and GL changes within one ACID transaction. The application is stateless and runs with a delivery/JWKS stub under Docker Compose.

Production extensions—including delivery, credit memos, void/reissue, write-off, intercompany elimination, full FX processing, manual journals, and period-management APIs—are designed but not represented as completed prototype features.

## 2. Data Model and Architecture

![Core ER diagram](er-diagram-core.svg)

Core entities:

- Tenant and Entity model company/subsidiary hierarchy.
- AppUser stores entity membership and roles.
- Customer is the external AR counterparty.
- Invoice and InvoiceLineItem store transaction/base amounts, tax, due date, lifecycle, balance, and optimistic version.
- Payment and PaymentAllocation support one payment across one or more invoices.
- GLAccount, JournalEntry, and JournalEntryLine form the accounting trail.
- AccountingPeriod controls posting dates.
- IdempotencyKey and AuditLog provide operational and compliance controls.

### Tenant and entity isolation

Tenant, user, and default entity come from the verified JWT rather than the request body. Application queries filter by tenant and financial transactions set transaction-local tenant/user context for PostgreSQL. Tables define tenant RLS policies.

Production hardening: run through a non-owner database role, enable `FORCE ROW LEVEL SECURITY`, validate current user-to-entity permission from the database, and add composite tenant-aware foreign keys and cross-tenant tests.

### Currency model

Invoices and payments store transaction currency, exchange rate, base currency, and base amounts. ExchangeRate stores dated rates. The required prototype exercises INR-to-INR at rate 1; full rate lookup, manual-review flags, and realized FX accounting are deferred bonus functionality.

### Audit model

Database triggers record old/new row values and transaction-local actor identity. This makes audit capture independent of individual application repository calls. Production would revoke audit UPDATE/DELETE and archive signed records to immutable storage.

## 3. Accounting Integration

Invoice approval posts:

```text
Dr 1200 Accounts Receivable  174,000
Cr 3100 Sales Revenue        150,000
Cr 2200 Tax Payable           24,000
```

A payment of INR 100,000 posts:

```text
Dr 1100 Cash                 100,000
Cr 1200 Accounts Receivable  100,000
```

The resulting invoice balance, aging total, and net GL AR are all INR 74,000. The health endpoint reports whether AR subledger and GL account 1200 match.

Payments support AUTO FIFO and MANUAL allocation in code. The integration path verifies AUTO partial allocation and cached idempotent retry. Overpayment liability and multi-currency allocation require further production hardening before being claimed complete.

Credit memo and write-off accounting are documented in [Functional Requirements](FRs.md) and [Financial Controls](financial-controls.md), but their endpoints are outside the required prototype.

## 4. State and Business Rules

The modeled invoice states are DRAFT, APPROVED, SENT, PARTIALLY_PAID, PAID, VOID, and WRITTEN_OFF.

Implemented transitions:

```text
DRAFT --authorized approval--> APPROVED
APPROVED/SENT/PARTIALLY_PAID --partial payment--> PARTIALLY_PAID
APPROVED/SENT/PARTIALLY_PAID --full payment--> PAID
```

Approval requires a separate approver, an OPEN document-date period, an idempotency key, and the current invoice version through `If-Match`. Approval and payment create their GL entries atomically. SENT is optional in the prototype because delivery is outside scope; approval establishes the receivable.

## 5. API Design and Verification

| Required API | Prototype route | Verification |
|---|---|---|
| Create invoice | `POST /api/v1/invoices` | Total and lines asserted |
| Retrieve invoice | `GET /api/v1/invoices/{id}` | Balance/history asserted |
| Approve invoice | `POST /api/v1/invoices/{id}/approve` | Status/version and GL asserted |
| Record payment | `POST /api/v1/payments` | Allocation/balance and retry asserted |
| Customer aging | `GET /api/v1/customers/{id}/aging` | INR 74,000 current bucket asserted |
| Invoice journals | `GET /api/v1/journal-entries?invoice={id}` | Two balanced entries and net AR asserted |

Operational extension: `GET /health` checks database access, materialized-view age, and AR-to-GL reconciliation.

Run the repeatable integration test:

```bash
docker compose up -d --build
./test_api.sh
```

The script seeds deterministic data, generates fresh development JWTs, derives the created invoice ID, uses fail-on-HTTP-error calls, performs financial assertions, and verifies that retrying a completed payment returns the original payment rather than creating a duplicate.

Control assertions also verify that another tenant receives 404 for the
invoice, an invoice creator receives 403 from approval, and reuse of a payment
idempotency key with a changed payload receives 409.

### Idempotency

The database scope is `(tenant_id, endpoint, key)` with a request hash. COMPLETED returns the cached status/body; PROCESSING returns conflict; the same key with a different body returns conflict. The claim, financial records, and cached response commit together. Payment reference uniqueness provides a second database-level duplicate defense.

### Bulk operations

Bulk processing is Phase 2. The API design proposes asynchronous job submission, per-item idempotency, bounded batches, progress/error reporting, and retry of failed items rather than one unbounded database transaction.

## 6. Financial Controls and Operations

Detailed analysis is in [Financial Controls and Compliance Analysis](financial-controls.md). Key controls are:

- ACID transaction boundaries for every financial operation.
- Server-owned Decimal calculations and database constraints.
- Optimistic invoice versioning and serialized payment allocation.
- Role checks and creator/approver segregation.
- Application plus database OPEN-period checks.
- Database-triggered audit with actor context.
- AR-to-GL reconciliation surfaced operationally.
- Point-in-time recovery, restore drills, backward-compatible migrations, and transactional outbox recommended for production.

## 7. Enterprise Experience

The required personal examples must be completed truthfully by the candidate in [Enterprise Experience Showcase](experience-showcase.md). That file contains prompts for the four exact assessment topics and is intentionally not populated with invented employers, responsibilities, or metrics.

## 8. AI Assistance and Time

AI tools used:

- Claude: interactive domain learning, requirements, and design exploration.
- GitHub Copilot: code assistance during prototype construction.
- Codex: repository review, implementation fixes, Docker debugging, and integration verification.

The project was expanded beyond the original 3–4 hour timebox as an interactive learning and interview-walkthrough artifact. The candidate should add truthful approximate time spent on design, prototype, controls, and experience sections in the README before submission.

## 9. Known Limits

- The integration suite verifies the required happy path and selected security/idempotency failures, but not a comprehensive concurrency matrix.
- RLS needs a non-owner runtime role plus `FORCE ROW LEVEL SECURITY` for production-grade defense in depth.
- Journal immutability and aggregate balancing need database privilege/constraint enforcement.
- The stock local PostgreSQL image does not schedule pg_cron; the integration test refreshes aging explicitly. Deployment design selects a single scheduled refresh owner.
- Full multi-currency, overpayment liability, intercompany, amendment, and period-management workflows are deferred.
