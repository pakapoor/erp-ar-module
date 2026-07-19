# Interview Walkthrough — ERP AR Module

Use this as the spoken path through the repository. Lead with the financial
correctness boundary, not with the number of technologies used.

## 10-Minute Architecture Walkthrough

### 0:00–1:00 — Problem and scope

> This is a multi-tenant Accounts Receivable module. The required path creates
> an invoice, approves it into the General Ledger, receives and allocates
> payments, reports aging, and exposes the journal trail. I optimized first for
> correct and auditable books, then for scale and operability.

Show [requirements-traceability.md](requirements-traceability.md). State that
the six required APIs are complete and tested. Separate implemented V1 behavior
from explicitly documented V2 work.

### 1:00–2:30 — System boundary

Show [high-level-design.md](high-level-design.md).

```text
Client → Envoy L7 Gateway → FastAPI AR Application → PostgreSQL
                                      ↓
                         transactional delivery_outbox
                                      ↓
                       publisher → SQS/DLQ → consumer → adapter
```

- Envoy is the only public AR entry point. It validates JWTs early, rate-limits
  and injects trace IDs.
- FastAPI independently re-validates the JWT and derives tenant, entity, user
  and roles from it.
- PostgreSQL is the financial source of truth. Redis is not used for financial
  locks, idempotency or balances.
- The prototype is a modular monolith so invoice, payment and GL changes can
  share ACID transactions.

### 2:30–4:00 — Data and tenant model

Show [data-model.md](data-model.md) and its ER diagrams.

- Tenant owns one or more legal entities; customers and financial records are
  scoped by tenant and entity.
- Application queries filter both scopes. PostgreSQL RLS is defense in depth;
  production still needs a non-owner runtime role and `FORCE RLS`.
- UUID keys avoid central ID allocation. Database uniqueness and check
  constraints reinforce application validation.
- Journal entries are the accounting trail; invoice/payment tables are the AR
  subledger. `/health` reconciles subledger AR to GL account 1200.

### 4:00–6:30 — Financial transaction

Walk through the transaction described in the next section. Emphasize where
each transaction commits and why external delivery is outside it.

### 6:30–7:30 — Concurrency and retry safety

- Write APIs use durable, entity-scoped idempotency keys and request hashes.
- Invoice approval uses `If-Match` plus an atomic version compare-and-swap.
- Payments use PostgreSQL `SERIALIZABLE` because two different payment rows can
  compete for the same set of invoice balances.
- AUTO and MANUAL race tests prove one commit, one retryable HTTP 409, and no
  partial financial records.

### 7:30–8:30 — Asynchronous delivery

- Approval commits the outbox event with the invoice and GL entry.
- A publisher relays it to Standard SQS after commit.
- Consumers delete SQS only after the adapter succeeds and PostgreSQL records
  delivery. The stable event ID is the downstream idempotency key.
- Three failed receives redrive to the DLQ. A CFO can requeue a DEAD event;
  retrying delivery never repeats approval or accounting.

### 8:30–9:30 — Reporting and multi-currency

- Invoice detail is a live indexed query because stale detail is unsafe during
  approval.
- Aging accepts bounded staleness, so pg_cron refreshes a materialized view
  concurrently while readers retain the previous complete snapshot.
- A separate worker fetches ECB data. Financial APIs use only approved,
  persisted rates and snapshot the chosen rate on each transaction.
- Foreign payments release AR at the invoice rate, post Cash at the payment
  rate, and put the INR difference into realized FX Gain/Loss.

### 9:30–10:00 — Proof and honest limits

Show [testing.md](testing.md), then run or quote:

```bash
./deploy.sh --no-build --no-install --test
```

The command deploys eight services and runs the required API suite, AUTO and
MANUAL payment races, credit-memo controls, and SQS/DLQ recovery. Close by
naming deferred production work: forced RLS, immutable DB privileges, managed
TLS/JWT rotation, DLQ alarms/retention, PITR restore proof and load testing.

## One Financial Transaction End-to-End

Use the deterministic INR example because the accounting is easy to verify.

### 1. Create invoice

Rahul calls `POST /api/v1/invoices` with two line items. The server—not the
client—calculates:

```text
Subtotal:  INR 150,000
Tax:       INR  24,000
Total/AR:  INR 174,000
```

Invoice and line items commit atomically as `DRAFT`, version 1. The
idempotency record commits in the same transaction.

### 2. Approve and post

Priya calls `POST /api/v1/invoices/{id}/approve` with `If-Match: 1`.

Controls: role check, creator ≠ approver, DRAFT state, current version and OPEN
invoice-date period. One transaction changes the invoice to APPROVED version
2, creates the balanced journal, completes idempotency and inserts the outbox:

```text
Dr 1200 Accounts Receivable  174,000
Cr 3100 Sales Revenue        150,000
Cr 2200 Tax Payable           24,000
```

### 3. Deliver asynchronously

After commit, the publisher sends the durable event to SQS. The consumer calls
the adapter with the event ID, records `sent_at`, changes APPROVED to SENT and
increments the invoice version. SQS or adapter downtime cannot reverse the
receivable or its journal.

### 4. Receive partial payment

Priya records INR 100,000. Payment, allocation, invoice balance/status and GL
commit together under serializable isolation:

```text
Dr 1100 Cash                 100,000
Cr 1200 Accounts Receivable  100,000
```

The invoice becomes PARTIALLY_PAID with INR 74,000 outstanding. A completed
idempotent retry returns the cached response; a concurrent conflicting payment
receives HTTP 409 and may retry with the same key.

### 5. Report and reconcile

- API2 shows invoice, payments, credits and status history.
- API5 places INR 74,000 in the current aging bucket.
- API6 returns the approval and payment journals; each balances.
- API7 confirms subledger AR INR 74,000 equals net GL AR INR 74,000.

## Five Strongest Tradeoffs

### 1. Modular monolith over microservices

Chosen because invoice/payment/allocation/GL operations need one ACID boundary
and the assessment team is small. Accepted cost: modules cannot scale or deploy
independently yet. Decompose only when a real ownership or scaling boundary
outweighs distributed-transaction complexity.

### 2. PostgreSQL consistency over Redis coordination

PostgreSQL owns balances, row/version checks, serializable payment allocation
and idempotency. This avoids a lossy external lock or a Redis/DB dual write.
Accepted cost: more pressure on the primary and occasional serializable aborts;
clients must retry conflicts with the same idempotency key.

### 3. Transactional outbox plus Standard SQS

Direct HTTP makes approval depend on notification. Direct SQS creates a
database/broker dual-write gap. The outbox makes the financial commit atomic;
SQS adds buffering, consumer scaling and DLQ redrive. Accepted cost:
at-least-once delivery and two operational stores, so downstream idempotency,
monitoring and retention are mandatory.

### 4. Live invoice detail, materialized aging

Invoice detail is live because an approver cannot act on stale balances or
versions. Aging is a portfolio report where five-minute staleness is acceptable,
so a concurrently refreshed materialized view protects the database at scale.
Accepted cost: two read strategies and an explicit freshness timestamp.

### 5. Persisted accounting rates over synchronous market lookup

A separate worker fetches and validates reference rates; financial requests use
approved stored rates and snapshot them. This gives reproducible journals and
keeps provider/network failure outside payment latency. Accepted cost: rates
may be intentionally stale within policy, and production needs a
Finance-approved provider rather than treating ECB as an executable bank rate.

## Likely Interviewer Questions

### Why not microservices?

The dominant boundary is financial atomicity, not independent deployment. A
modular monolith is simpler and safer for this scope; the outbox already creates
a future asynchronous boundary for delivery.

### Why does API3 succeed when SQS is down?

Approval, GL and the outbox event commit in PostgreSQL. Notification is not the
financial source of truth. The publisher retries after SQS recovers.

### What if SQS accepts a message and the publisher crashes before PUBLISHED?

The PROCESSING lease expires and another publisher republishes the event. That
can create a duplicate, which is safe because the database and adapter both use
the stable event ID for idempotency.

### Why Standard SQS rather than FIFO?

There is one approval-delivery event per invoice and no cross-invoice ordering
requirement. FIFO would not remove the need for idempotent external side
effects, so Standard is the simpler honest choice.

### Why serializable payments if invoices have versions?

A payment transaction can read and allocate across several invoice rows while
another distinct payment changes that eligible set. A version protects one
known row; serializable protects the multi-row allocation decision.

### What does the idempotency key protect?

It protects retries of the same command. It is scoped by tenant, entity,
endpoint and key; the request hash rejects key reuse with a different payload.
Status/version checks and database constraints independently protect business
state.

### Why return 409 for a serialization failure?

The request was valid, but it conflicted with concurrent state. HTTP 409 tells
the client to retry the same logical operation with the same idempotency key.

### Why check the invoice-date accounting period?

Approval posts the invoice to its document period. CLOSED requires an audited
CFO reopen; LOCKED is irreversible and corrections use an approved current-
period adjustment while preserving the original document date.

### Can RLS currently guarantee isolation by itself?

Not completely: policies exist, but the prototype runtime owner can bypass
them. Application tenant/entity predicates are tested. Production uses a
non-owner role, `FORCE ROW LEVEL SECURITY`, tenant-aware foreign keys and
negative DB tests.

### Why can an approved invoice be paid before delivery succeeds?

Approval establishes the receivable and GL posting. Delivery is a communication
side effect. Blocking payment because an email or EDI adapter failed would make
operational notification state incorrectly control financial truth.

### What happens to an overpayment?

Cash is debited for the full receipt, AR is credited only for the allocated
amount, and the unapplied balance credits Customer Credit liability. It never
credits nonexistent AR or revenue.

### What would you build next for production?

First harden controls and operations: non-owner forced RLS, immutable audit and
posted-journal privileges, managed asymmetric JWT/TLS, DLQ alarms/retention,
versioned migrations, PITR restore drills and representative load tests.

## Safe Live-Change Exercises

Use a new branch. Run the smallest focused test first, then the full suite. Do
not reset the database volume or edit posted financial rows manually.

### Exercise 1 — Add an optional invoice field

Example: `sales_order_reference`.

1. Add a nullable migration and ORM field.
2. Add it to request/response schemas.
3. Persist it in API1 and return it in API2.
4. Add one integration assertion.

This demonstrates backward-compatible schema evolution without changing
accounting.

### Exercise 2 — Add a delivery-status filter

Example: allow `GET /invoices/{id}/delivery?status=DEAD`.

1. Add a validated optional query parameter in `src/routers/delivery.py`.
2. Apply the predicate inside the existing tenant/entity scope.
3. Test valid filtering, invalid status and cross-entity concealment.

This is operationally useful and low risk.

### Exercise 3 — Add a delivery metric to health

Example: count DEAD events and expose the oldest age without making health fail
for a single event.

1. Query within a bounded indexed status range.
2. Separate informational degradation from DB/reconciliation failure.
3. Test zero and nonzero cases.

Discuss why alert policy belongs outside the financial transaction.

### Exercise 4 — Add a business validation

Example: reject an invoice with more than 100 line items.

1. Put the bound in the request schema.
2. Add boundary tests for 100 and 101.
3. Verify a rejected request leaves no invoice, lines or idempotency success.

This demonstrates server-owned invariants and atomic rollback.

### Exercise 5 — Add cursor pagination to an operational list

Do this on a new cross-invoice delivery-event list, not the invoice-scoped
journal API whose result set is deliberately small.

1. Order by `(created_at, id)`.
2. Encode both values in an opaque cursor.
3. Preserve tenant/entity predicates.
4. Test stable ordering with equal timestamps and no duplicates.

### Avoid as an unplanned live change

- Changing debit/credit formulas without a reconciliation test
- Cross-currency settlement or unrealized revaluation
- Reopening/locking accounting periods
- Replacing PostgreSQL concurrency with an in-memory lock
- Converting the modular monolith to microservices
- Destructive migration or Docker volume reset

For any financial change, state the invariant first, identify the transaction
boundary, implement the smallest change, and prove both journal balance and
AR-to-GL reconciliation.
