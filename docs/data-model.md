# Data Model — ERP AR Module

## Table of Contents
- [Entity List](#entity-list)
- [ER Diagram — Core Entities](#er-diagram--core-entities)
- [ER Diagram — Supporting Entities](#er-diagram--supporting-entities)
- [Table Schemas](#table-schemas)
- [Key Design Decisions](#key-design-decisions)

---

## Entity List

### Core Entities (system cannot function without these)

| Entity | Description |
|--------|-------------|
| Tenant | ERP customer — owns all data, one contract |
| Entity | Legal subsidiary within a tenant |
| Customer | External company being invoiced |
| Invoice | The bill sent to customer |
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
| Credit Memo | Correction document against an invoice |

---

## ER Diagram — Core Entities

![Core entities ER diagram](er-diagram-core.svg)

---

## ER Diagram — Supporting Entities

![Supporting entities ER diagram](er-diagram-supporting.svg)

---

## Table Schemas

Full schema with all constraints, indexes, and RLS policies:
[migrations/001_initial_schema.sql](../migrations/001_initial_schema.sql)

Key design decisions in the schema:
- UUID primary keys on all tables
- Row Level Security on every table (tenant isolation at DB level)
- Audit triggers fire automatically — cannot be bypassed by application code
- Journal entries are immutable — no UPDATE/DELETE ever
- SOX segregation enforced via CHECK constraint: invoice creator != approver
- Period close enforced via DB trigger — blocks posting to closed periods
- Idempotency keys table prevents duplicate payment processing
- AR Aging as materialized view — refreshed every 5 minutes

---

## Key Design Decisions
Coming soon
