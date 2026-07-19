# Financial Controls and Compliance Analysis

This document distinguishes controls implemented in the prototype from controls recommended for production. The prototype favors correctness and auditability over availability: a financial write fails as one transaction rather than leaving the AR subledger and GL inconsistent.

## 1. Data Integrity

### Atomic financial transactions

Invoice creation stores the invoice, its line items, audit records, and idempotency result in one PostgreSQL transaction. Approval updates the invoice and creates the journal entry and lines in one transaction. Payment recording creates the payment, allocations, invoice balance changes, GL entry, audit records, and cached idempotent response in one transaction. Any error rolls back the complete operation.

This prevents states such as a payment existing without an allocation, or an approved invoice without its GL entry.

### Server-owned calculations

The server calculates line subtotal, line tax, line total, invoice subtotal, total tax, grand total, balance, and due date. Client-supplied totals are not accepted. PostgreSQL additionally checks:

- `total_amount = subtotal_amount + tax_amount`
- `0 <= balance_amount <= total_amount`
- `due_date >= invoice_date`
- positive payment and allocation amounts
- `unallocated_amount = amount - allocated_amount`
- exactly one debit or credit side is positive for each journal line

Money uses `DECIMAL`, never binary floating point.

### AR-to-GL reconciliation

Invoice approval produces:

```text
Dr Accounts Receivable  total_amount
Cr Sales Revenue        subtotal_amount
Cr Tax Payable          tax_amount
```

Payment application produces:

```text
Dr Cash                 payment amount
Cr Accounts Receivable  allocated amount
```

The `/health` endpoint compares the total open invoice balance with the net balance of GL account `1200`. A mismatch makes the service unhealthy and is intended to trigger an operational alert. The integration test verifies that the sample invoice leaves both the subledger and GL at INR 74,000.

Production hardening: enforce aggregate journal balancing with a deferred database constraint trigger, and run scheduled reconciliation independently of the API health probe so failures are persisted and alerted even when no probe is running.

### Duplicate payment prevention

Two independent controls are used:

1. `X-Idempotency-Key` is claimed in PostgreSQL in the same transaction as the financial write. A completed retry returns the original status and body; an in-flight retry returns conflict; reuse with a different payload is rejected.
2. `UNIQUE (tenant_id, customer_id, payment_reference)` prevents the same bank reference being imported twice even with a different idempotency key.

Payment allocation uses `SERIALIZABLE` isolation. A concurrent conflicting allocation is aborted rather than silently double-applying funds. Production clients should retry serialization failures using the same idempotency key.

## 2. Invoice Lifecycle and Amendments

The modeled lifecycle is:

```text
DRAFT -> APPROVED -> SENT -> PARTIALLY_PAID -> PAID
   |          |
   +-> VOID <-+
APPROVED/SENT/PARTIALLY_PAID -> WRITTEN_OFF
```

The prototype implements DRAFT creation, approval, asynchronous delivery to SENT, and payment-driven PARTIALLY_PAID/PAID transitions. Payments are allowed against APPROVED as well as SENT invoices because approval establishes the receivable and delivery may still be retrying.

An approved invoice must not be edited in place. Corrections should use:

- Void and reversing GL entry when the invoice should never have existed and no payment was applied.
- Credit memo when price, quantity, returned goods, or tax must be corrected after issue.
- Write-off for an uncollectible outstanding balance; already received cash is never reversed.

Credit memo, void, and write-off routes now exist as bonus prototype code and
create their state/GL changes atomically. They are deliberately labelled
experimental rather than production-hardened. Repeatable INR credit-memo,
DRAFT-void and write-off happy paths now pass, including post-command AR-to-GL
reconciliation. The suite does not yet prove entity isolation, foreign-currency
behavior, concurrent changes, idempotent retries, or posted-void reversal.
Void-and-reissue orchestration remains Phase 2.

## 3. Audit and SOX Controls

### Segregation of duties

JWT roles separate invoice creation, approval, and payment recording. Approval requires `invoice_approver` or `cfo`, and the application rejects `created_by == approved_by`. PostgreSQL repeats this control with the invoice SOX check constraint.

Approval uses `If-Match` with the invoice version. Every mutation increments the version, so a stale approval is rejected instead of overwriting an intervening change.

### Audit trail

Database triggers capture INSERT, UPDATE, and DELETE events with tenant, table, record, action, old value, new value, actor, and timestamp. The application sets `app.current_user_id` and `app.tenant_id` as transaction-local PostgreSQL context before financial writes, so trigger records carry the verified JWT actor.

The prototype does not yet make the audit table physically immutable. Production controls should:

- revoke UPDATE and DELETE from application roles;
- use a separate append-only audit owner;
- capture trace/session/IP metadata;
- archive signed records to immutable object storage with retention policy;
- monitor gaps in audit sequence and failed archival.

### Journal immutability

Posted entries are corrected by reversal, not update. This is currently an application and schema-design policy. Production should enforce it with database privileges or triggers that reject UPDATE/DELETE for posted journal entries and lines.

## 4. Period Close

Accounting periods have OPEN, CLOSED, and LOCKED states:

- OPEN accepts normal posting.
- CLOSED can be reopened only by an authorized CFO with actor, timestamp, and reason.
- LOCKED is irreversible after audit or tax filing.

Invoice approval checks the period containing `invoice_date` in application code. Payment posting checks the period containing `payment_date`. A database trigger independently verifies that every journal entry posts to its referenced OPEN period. Period transition constraints prevent reopening LOCKED periods and prevent direct OPEN-to-LOCKED transitions.

For a correction to a LOCKED period, production creates a new `PRIOR_PERIOD_ADJUSTMENT` in a current OPEN period while preserving:

- original `document_date`;
- current-period `entry_date`;
- adjusted locked-period reference;
- mandatory reason;
- CFO approval;
- creator and approver separation.

The schema supports these fields and constraints; period-management and manual-adjustment APIs are Phase 2.

## 5. Multi-Tenant Security

Tenant and user identity come from a verified JWT, never from financial request bodies. Application queries filter by tenant, and PostgreSQL tables define tenant RLS policies. Financial transactions set tenant context locally before database work.

Production hardening should use a non-owner application database role and `FORCE ROW LEVEL SECURITY`, validate current user-to-entity authorization from the database, use composite tenant-aware foreign keys where practical, and add explicit cross-tenant integration tests.

## 6. Operational Resilience

### Deployment without financial data loss

Use backward-compatible expand/migrate/contract database changes:

1. Add nullable columns or new tables and deploy code that understands both versions.
2. Backfill online in bounded batches.
3. switch reads/writes after verification.
4. Add constraints and remove old fields in a later release.

Deploy stateless application instances behind a load balancer with rolling or blue/green replacement. Stop routing to an instance before shutdown and allow in-flight transactions to finish. Database migrations run once, separately from horizontally scaled application startup.

### Backup and recovery

Production PostgreSQL should use encrypted automated snapshots plus continuous WAL archiving for point-in-time recovery. Backups are incomplete until restore drills prove recovery. Define and test financial RPO/RTO, retain audit data according to policy, replicate across failure domains, and reconcile AR to GL after recovery before reopening writes.

### Failure during payment

If an application instance fails before commit, PostgreSQL rolls back payment, allocations, balance changes, journal lines, audit rows, and the idempotency claim. The client retries with the same key. If failure occurs after commit but before the response arrives, the retry reads and returns the cached committed response without applying the payment again.

External delivery does not occur inside the financial transaction. Approval commits a transactional outbox row with the invoice and GL entry. A separate idempotent worker retries delivery afterward; exhausted events enter DEAD status for operational handling. Therefore delivery failure never reverses a committed financial transaction.

## 7. Prototype Evidence

Run:

```bash
docker compose up -d --build
./test_api.sh
```

The script generates fresh JWTs, seeds deterministic data, exercises the six required APIs plus health, verifies cached payment retry, checks balanced journal entries, and asserts that invoice balance, AR aging, and GL net AR all equal INR 74,000.
