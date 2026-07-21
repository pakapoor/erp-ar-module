# Data Model -- ERP AR Module

## Table of Contents
- [Entity List](#entity-list)
- [ER Diagram -- Core Entities](#er-diagram--core-entities)
- [ER Diagram -- Supporting Entities](#er-diagram--supporting-entities)
- [Table Schemas](#table-schemas)
- [Key Design Decisions](#key-design-decisions)

---

## Entity List

### Core Entities (system cannot function without these)

| Entity | Description |
|--------|-------------|
| Tenant | ERP customer -- owns all data, one contract |
| Entity | Legal subsidiary within a tenant |
| Customer | External company being invoiced |
| Invoice | The bill sent to customer; `rejection_reason` set (status returns to DRAFT) when an approver rejects instead of approving |
| Invoice Line Item | Individual line on an invoice |
| Payment | Money received from customer |
| Journal Entry | Accounting record of financial event |

### Supporting Entities

| Entity | Description |
|--------|-------------|
| GL Account | Chart of accounts for a tenant |
| Journal Entry Line | Individual debit/credit line in a journal entry |
| Payment Allocation | Links payment to one or more invoices |
| User | Person using the system (RBAC) |
| Audit Log | Immutable record of all changes (SOX) |
| Accounting Period | Month/year close tracking |
| Exchange Rate | Daily currency conversion rates |
| Credit Memo | Correction document against an invoice; `replacement_invoice_id` optionally links to a reissued invoice |
| AR Aging (materialized view) | Current derived snapshot by tenant, entity, and customer; refreshed every 5 minutes |
| Idempotency Key | Tenant- and endpoint-scoped write claim with request hash and cached response |
| Delivery Outbox | Durable invoice-delivery event and DB->SQS publication lifecycle |
| FX Import Job | Durable pg_cron request claimed by the FX worker; retry and provenance boundary |

---

## ER Diagram -- Core Entities

![Core entities ER diagram](er-diagram-core.svg)

Dashed lines mark relationships that are not a direct foreign key: Invoice-Payment
resolves many-to-many through the Payment Allocation bridge table, Invoice/Payment-
Journal Entry is a polymorphic `reference_type`/`reference_id` pair rather than an
FK, and GL Account-Journal Entry resolves through Journal Entry Line. See the
supporting diagram for all three intermediate tables.

---

## ER Diagram -- Supporting Entities

![Supporting entities ER diagram](er-diagram-supporting.svg)

---

## Table Schemas

Full schema begins in [migrations/001_initial_schema.sql](../migrations/001_initial_schema.sql);
the delivery outbox is added by
[migrations/004_delivery_outbox.sql](../migrations/004_delivery_outbox.sql) and
extended with SQS publication metadata by
[migration 009](../migrations/009_sqs_delivery_pipeline.sql), while
the FX import/provenance foundation by
[migrations/006_fx_rate_ingestion.sql](../migrations/006_fx_rate_ingestion.sql),
base-currency aging by migration 007, and base-only realized-FX journal lines
by migration 008.

The completed V1 FX extension is specified in
[FX Rate Ingestion and Multi-Currency Design](fx-rate-design.md). Migration 006
implements `fx_import_job`, rate provenance/approval fields, immutable
supersession, and transaction-to-rate references. APIs snapshot transaction and
base values; payment allocations retain payment-rate value, AR carrying value
and the resulting realized gain/loss.

Key design decisions in the schema:
- UUID primary keys on all tables
- Row Level Security on every table (tenant isolation at DB level)
- Audit triggers fire automatically -- cannot be bypassed by application code
- Journal entries are immutable -- no UPDATE/DELETE ever
- SOX segregation enforced via CHECK constraint: invoice creator != approver
  (the same constraint covers rejection, since reject/approve share one endpoint)
- Rejection reuses the invoice row rather than a separate table: `approve`
  with `action: REJECT` returns status to DRAFT, stores `rejection_reason`,
  and bumps `version`; there is no dedicated `rejected_by`/`rejected_at`
  column, so who rejected and when live only in `audit_log`. A subsequent
  PATCH (creator only, DRAFT only) clears `rejection_reason` on save
- Period close enforced via DB triggers -- CLOSED requires an audited reopen;
  LOCKED is irreversible, and adjustments post to a current OPEN period while
  preserving the original document date
- Idempotency keys table prevents duplicate writes and safely replays completed
  responses; payment references provide a second database uniqueness guard
- Delivery outbox provides atomic approval/event persistence, multi-publisher
  safe claims, broker message metadata, separate publication/delivery attempt
  counters, DEAD recovery state, and a stable downstream idempotency identifier
- The lifecycle is PENDING -> PROCESSING -> PUBLISHED -> DELIVERING -> DELIVERED;
  publication or delivery exhaustion produces DEAD. Standard SQS/DLQ remains
  transport state, while PostgreSQL remains the operational source of truth.
- AR Aging as a current-only materialized view -- refreshed every 5 minutes;
  historical reporting requires event reconstruction or persisted snapshots
- Foreign-currency documents snapshot both the approved rate ID and numeric
  rate; approved rate corrections insert a superseding row rather than changing
  historical transactions
- Journal lines retain both document and base amounts. A realized-FX line may
  be base-only (zero on both document sides) but must have exactly one positive
  base debit/credit side; migration 008 enforces this shape

---

## Key Design Decisions

- **No dedicated Rejection table.** Reject is a state on Invoice
  (`rejection_reason` + status back to DRAFT + version bump), not a new
  entity, because a rejected invoice is the same draft being corrected, not
  a new financial fact. The identity of the rejector is not a first-class
  column; it is recovered from `audit_log` if needed.
- **Payment does not FK to Invoice directly.** A single payment can settle
  multiple invoices (or partially settle one), so the relationship is
  many-to-many through `payment_allocation`, which also carries the
  per-invoice FX gain/loss.
- **Journal Entry links to its source via a polymorphic reference, not a
  set of nullable FKs.** `reference_type` + `reference_id` avoids one
  nullable FK column per possible source (invoice, payment, credit memo,
  write-off, manual, prior-period adjustment, FX revaluation) and keeps
  the journal table source-agnostic; `idx_je_reference` makes the reverse
  lookup indexed.
- **GL Account never appears on Journal Entry itself.** Each entry can
  touch multiple accounts (one debit, N credits or vice versa), so the
  account reference lives on `journal_entry_line`, keeping the header
  immutable and the lines the only place account-level amounts exist.
