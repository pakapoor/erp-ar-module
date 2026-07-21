# Principal Engineer ERP Assessment -- Consolidated Submission

## 1. Scope and Design Position

This prototype implements the six APIs required by the assessment and demonstrates one complete financial path: create an invoice, retrieve it, approve it, generate the receivable GL entry, apply a partial payment, report aging, and retrieve the resulting journal trail.

The architecture is a modular monolith implemented with FastAPI and PostgreSQL behind an Envoy L7 gateway. This keeps invoice, payment, allocation, audit, GL, and delivery-outbox writes within ACID transaction boundaries. Envoy rejects invalid JWTs early, injects trace IDs, and is the only public AR entry point; the internal FastAPI application independently re-validates JWTs. The application runs with an outbox publisher, LocalStack Standard SQS/DLQ, an idempotent delivery consumer, and a delivery/JWKS stub under Docker Compose.

Production delivery adapters (Email/EDI/IRP), void-and-reissue orchestration,
intercompany elimination, manual journals, period-management APIs and period-end
FX revaluation remain deferred. Full V1 FX ingestion, invoice/payment posting,
realized gain/loss and failure controls are implemented and tested. Bonus credit
memo, write-off and void routes have repeatable INR happy-path/reconciliation
tests, but remain `IN PROGRESS` until their extended financial-control matrix
passes. The durable delivery
outbox, SQS/DLQ pipeline, operations endpoints, and console stub are implemented
with a focused repeatable test.

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
- DeliveryOutbox closes the DB/broker dual-write gap; a publisher relays durable
  events to Standard SQS and an idempotent consumer invokes the adapter.

### Tenant and entity isolation

Tenant, user, and entity come from the verified JWT rather than the request body. Application queries filter by both tenant and entity, financial transactions set transaction-local tenant/entity/user context for PostgreSQL, and idempotency caches are entity-scoped. Cross-tenant and cross-entity API tests verify concealed 404 responses. Tables define tenant RLS policies.

Production hardening: run through a non-owner database role, enable `FORCE ROW LEVEL SECURITY`, validate current user-to-entity permission from the database, and add composite tenant-aware foreign keys and cross-tenant tests.

### Currency model

Invoices and payments store transaction currency, locked exchange-rate ID/rate,
base currency, and base amounts. A scheduled worker derives approved foreign->INR
rates from ECB data. The integration suite verifies USD invoice approval,
partial/final settlement, realized gain/loss, stale-rate failure and rejection
of unsupported cross-currency allocation. Period-end unrealized revaluation is
deferred.

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

Payments support AUTO FIFO and MANUAL allocation. The integration path verifies
partial allocation, cached idempotent retry, balanced overpayment accounting,
foreign-currency settlement and realized FX. Unapplied cash is credited to the
2100 Customer Credit liability account. A separate race suite submits two
different full receipts in each allocation mode and proves one commit, one
retryable conflict and one financial posting.

Credit memo, write-off and void accounting are documented in
[Functional Requirements](FRs.md) and [Financial Controls](financial-controls.md).
Credit-memo B6 controls are complete. Write-off and void retain basic INR
happy-path and reconciliation coverage and are not represented as complete
until their extended control matrices pass.

## 4. State and Business Rules

The modeled invoice states are DRAFT, APPROVED, SENT, PARTIALLY_PAID, PAID, VOID, and WRITTEN_OFF.

Implemented transitions:

```text
DRAFT --authorized approval--> APPROVED
APPROVED --successful asynchronous delivery--> SENT
APPROVED/SENT/PARTIALLY_PAID --partial payment--> PARTIALLY_PAID
APPROVED/SENT/PARTIALLY_PAID --full payment--> PAID
```

The bonus credit-memo path is fully verified for entity isolation, FX,
paid/partial balances, concurrency, idempotency, journal visibility and
reconciliation. CFO write-off and DRAFT/APPROVED/SENT void paths remain only
partially verified and do not widen the required core lifecycle claim above.

Approval requires a separate approver, an OPEN document-date period, an idempotency key, and the current invoice version through `If-Match`. Approval and payment create their GL entries atomically. Approval also writes a delivery outbox event in the same transaction; the publisher and SQS consumer later record SENT without making notification success a prerequisite for the receivable. Three failed receives reach a DLQ, and CFO retry changes only delivery state.

## 5. API Design and Verification

| Required API | Prototype route | Verification |
|---|---|---|
| Create invoice | `POST /api/v1/invoices` | Total and lines asserted |
| Retrieve invoice | `GET /api/v1/invoices/{id}` | Balance/history asserted |
| Approve invoice | `POST /api/v1/invoices/{id}/approve` | Status/version and GL asserted |
| Record payment | `POST /api/v1/payments` | Allocation/balance and retry asserted |
| Customer aging | `GET /api/v1/customers/{id}/aging` | INR 74,000 current bucket asserted |
| Invoice journals | `GET /api/v1/journal-entries?invoice_id={id}` | Two balanced entries and net AR asserted |

Bonus credit-memo controls are complete. DRAFT-void and write-off happy paths
pass with final reconciliation, but their extended controls remain
`IN PROGRESS`, separate from the required API table's completion claim.

Operational extension: `GET /health` checks database access, materialized-view age, and AR-to-GL reconciliation.

Delivery operations add `GET /invoices/{id}/delivery` and CFO-only
`POST /delivery-events/{id}/retry`. The focused suite proves the happy path,
entity isolation, at-least-once deduplication, DLQ failure path and recovery
without repeating financial entries.

The complete procedure, expected response values, gateway isolation/JWT checks, database inspection queries,
pg_cron proof, delivery logs, and retry drill are documented in
[Verification and Expected Results](testing.md).

Run the repeatable integration test:

```bash
./deploy.sh --test
```

The script seeds deterministic data, generates fresh development JWTs, derives the created invoice ID, uses fail-on-HTTP-error calls, performs financial assertions, and verifies that retrying a completed payment returns the original payment rather than creating a duplicate.

Control assertions also verify tenant/entity isolation, entity-scoped cached
responses, creator/approver separation, overpayment liability accounting,
changed-payload idempotency rejection, stale versions, a simultaneous approval
race, and simultaneous AUTO/MANUAL payment-allocation races.

### Idempotency

The database scope is `(tenant_id, entity_id, endpoint, key)` with a request hash. COMPLETED returns the cached status/body; PROCESSING returns conflict; the same key with a different body returns conflict. The claim, financial records, and cached response commit together. Payment reference uniqueness provides a second database-level duplicate defense.

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
- Point-in-time recovery, restore drills, and backward-compatible migrations; the implemented transactional outbox is the base for production delivery adapters.

## 7. Enterprise Experience

The required personal examples must be completed truthfully by the candidate in [Enterprise Experience Showcase](experience-showcase.md). That file contains prompts for the four exact assessment topics and is intentionally not populated with invented employers, responsibilities, or metrics.

## 8. AI Assistance and Time

AI tools used:

- Claude: interactive domain learning, requirements, and design exploration.
- GitHub Copilot: code assistance during prototype construction.
- Codex: repository review, implementation fixes, Docker debugging, and integration verification.

The project was expanded beyond the original 3--4 hour timebox as an interactive learning and walkthrough artifact. The candidate should add truthful approximate time spent on design, prototype, controls, and experience sections in the README before submission.


## 9. Accounting Standards and Assumptions

### GAAP vs IFRS Position

This prototype is designed to be standards-neutral at the data model level -- the schema stores both transaction currency and base currency amounts, captures document date separately from posting date, and supports the period-close and prior-period correction workflows required under both GAAP and IFRS. The following explicit assumptions apply:

**Revenue recognition:** The prototype assumes point-in-time revenue recognition under ASC 606 (GAAP) / IFRS 15 -- revenue is recognised when the invoice is approved, meaning the performance obligation (goods or services delivered) is treated as satisfied at that moment. Deferred revenue for multi-period or subscription arrangements requires a separate deferred revenue GL account and recognition schedule, which is designed but not implemented in this prototype (see Section 10 below).

**Receivables:** AR is recorded at invoice total (net of tax under IFRS, gross under US GAAP where tax is a liability). This prototype uses the IFRS-aligned approach -- revenue is credited at the subtotal amount and tax payable is a separate liability credit -- which also aligns with GST/VAT treatment in Indian mid-market ERP (the scenario currency is INR).

**Foreign currency:** Monetary items (AR, cash) are retranslated at the closing rate under IAS 21 / ASC 830. The prototype locks invoice- and payment-date rates and posts realised FX gain/loss on settlement. Full retranslation at period-end closing rates is a production requirement documented in fx-rate-design.md but not implemented in the prototype.

**Period close:** The OPEN/CLOSED/LOCKED period model aligns with both GAAP and IFRS requirements. No entries post to a closed period; prior-period adjustments in a locked period use a current-period adjustment entry preserving the original document date -- consistent with IAS 8 / ASC 250 requirements for correction of errors.

**Assumption on tax:** Tax collected from customers (GST/VAT) is a liability, not revenue. The GL entry on approval credits Tax Payable (2200), not Revenue -- consistent with both GAAP and IFRS treatment of pass-through taxes.

---

## 10. Revenue Recognition -- V2 Decision

### Current Implementation

The prototype implements **point-in-time recognition** under ASC 606 / IFRS 15 Performance Obligation Model: when an invoice is approved, the full invoice subtotal is recognised as revenue immediately. This is correct for straightforward goods and services invoicing where the performance obligation is satisfied on delivery.

GL entry on approval:
```
Dr  Accounts Receivable  174,000   (total including tax)
Cr  Sales Revenue        150,000   (subtotal -- performance obligation satisfied)
Cr  Tax Payable           24,000   (government liability, not revenue)
```

### Deferred Revenue (approved V2 direction)

For subscription, retainer, or multi-period service contracts, revenue must be deferred and recognised over the service period. The production design is:

1. On invoice approval, credit **Deferred Revenue** (liability) instead of Sales Revenue:
```
Dr  Accounts Receivable  12,000   (annual subscription)
Cr  Deferred Revenue     12,000   (liability -- not yet earned)
```

2. Monthly recognition job transfers earned portion to revenue:
```
Dr  Deferred Revenue     1,000   (1/12 recognised)
Cr  Sales Revenue        1,000   (earned this period)
```

3. The invoice line item requires a `recognition_type` field (IMMEDIATE or OVER_TIME), a `recognition_start_date`, `recognition_end_date`, and a `recognition_schedule` that drives the monthly journal.

4. The AR aging and balance sheet must distinguish between billed AR (cash expected) and unearned revenue (service still owed) -- these are separate concerns that the Tenant -> Entity -> Period model supports.

This is a deliberate V2 decision, not an incomplete V1 claim. The required
prototype remains scoped to invoices for goods or services whose performance
obligation is satisfied by approval. A subscription or other over-time line
will instead credit Deferred Revenue and use a persisted recognition schedule;
it must not credit the full amount to Sales Revenue on approval.

Before V2 implementation, Finance must approve the performance-obligation and
allocation policy, schedule granularity, contract-modification/cancellation
rules, period-close behavior and cumulative catch-up treatment. Deferring the
code avoids presenting a simplistic monthly job as ASC 606/IFRS 15 compliance.

## 11. Intercompany Elimination -- V2 Decision

An intercompany invoice produces valid entries in two legal-entity ledgers:
the seller records Intercompany AR and Revenue, while the buyer records an
Expense or Asset and Intercompany AP. Those statutory entries are not netted or
rewritten.

V2 will match both sides through an intercompany transaction identifier and
post elimination entries to a separate consolidation ledger. A consolidated
report combines both entity ledgers and the elimination ledger, removing the
internal AR/AP and Revenue/Expense without changing either entity's books.
Unmatched amounts, currencies or periods enter a reconciliation workflow; they
are not silently forced to balance.

The existing `is_intercompany` and `receiver_entity_id` invoice fields are
preparatory schema only. Matching, buyer-side AP integration, elimination
batches, consolidation reporting and their controls are not implemented in the
prototype.

## 12. Known Limits

- The integration suite verifies the required path, FX accounting, entity/tenant
  isolation, overpayment, stale versions, simultaneous approval, and real AUTO
  and MANUAL full-payment races. This is a deterministic two-request control,
  not a representative production load test.
- Write-off and void routes require dedicated entity, currency, concurrency,
  idempotency, balanced-journal and reconciliation tests before completion.
- RLS needs a non-owner runtime role plus `FORCE ROW LEVEL SECURITY` for production-grade defense in depth.
- Journal immutability and aggregate balancing need database privilege/constraint enforcement.
- The custom PostgreSQL image loads pg_cron. One job owned by the `postgres` system database refreshes `erp_db.ar_aging` concurrently every five minutes; the integration test also refreshes explicitly for deterministic assertions.
- Intercompany, void/reissue orchestration, period-management workflows,
  cross-currency settlement and unrealized FX revaluation are deferred.
