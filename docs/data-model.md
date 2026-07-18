# Data Model — ERP AR Module

## Table of Contents
- [Entity List](#entity-list)
- [ER Diagram](er-diagram.md)
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
