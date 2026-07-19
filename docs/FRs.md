# Functional Requirements — ERP AR Module

## Table of Contents
- [FR1 — Invoice Creation](#fr1--invoice-creation)
- [FR2 — Invoice Approval](#fr2--invoice-approval)
- [FR3 — Invoice Sending](#fr3--invoice-sending)
- [FR4 — Payment Recording and Allocation](#fr4--payment-recording-and-allocation)
- [FR5 — Credit Memo](#fr5--credit-memo)
- [FR6 — Write-off](#fr6--write-off)
- [FR7 — Invoice Void](#fr7--invoice-void)
- [FR8 — AR Aging Report](#fr8--ar-aging-report)
- [FR9 — GL Journal Entries and Reconciliation](#fr9--gl-journal-entries-and-reconciliation)
- [FR10 — Multi-tenant Isolation](#fr10--multi-tenant-isolation)
- [FR11 — Multi-currency](#fr11--multi-currency)
- [FR12 — Audit Trail](#fr12--audit-trail)
- [FR13 — Accounting Period Enforcement](#fr13--accounting-period-enforcement)
- [FR14 — RBAC and Segregation of Duties](#fr14--rbac-and-segregation-of-duties)
- [Future-FR1 — Intercompany Consolidation](#future-fr1--intercompany-consolidation)
- [Future-FR2 — Manual Journal Entry](#future-fr2--manual-journal-entry)
- [Future-FR3 — Period Management APIs](#future-fr3--period-management-apis)
- [Future-FR4 — User Management APIs](#future-fr4--user-management-apis)

---

## FR1 — Invoice Creation

System must allow creation of an invoice with sender entity, receiver (customer) details, line items (description, quantity, unit price, tax rate, tax jurisdiction, tax amount, line total), subtotal, total tax, and grand total. Optional PO reference. Currency at invoice level. Each line item supports its own tax rate and tax jurisdiction.

```
POST /invoices

Invoice #1001
Sender:   Reliance Retail, Mumbai (GSTIN: 27AAACR...)
Receiver: Tata Steel, Bangalore (GSTIN: 29AAACT...)
PO Ref:   PO-2024-789

Line Items:
| Description      | Qty | Price    | Tax  | Juris  | Total     |
|------------------|-----|----------|------|--------|-----------|
| Industrial Pump  |  2  | ₹50,000  | 18%  | MH     | ₹1,18,000 |
| Safety Valves    | 10  | ₹5,000   | 12%  | KA     | ₹56,000   |

Subtotal:  ₹1,50,000
Tax:       ₹24,000
Total:     ₹1,74,000
Currency:  INR
```

```
GET /invoices/1001

Response:
Invoice #1001
Status:   Partially Paid
Sender:   Reliance Retail, Mumbai (GSTIN: 27AAACR...)
Receiver: Tata Steel, Bangalore (GSTIN: 29AAACT...)

Line Items:
| Description      | Qty | Price    | Tax  | Juris  | Total     |
|------------------|-----|----------|------|--------|-----------|
| Industrial Pump  |  2  | ₹50,000  | 18%  | MH     | ₹1,18,000 |
| Safety Valves    | 10  | ₹5,000   | 12%  | KA     | ₹56,000   |

Subtotal:        ₹1,50,000
Tax:             ₹24,000
Invoice Total:   ₹1,74,000
Paid:            ₹1,00,000
Credit Memos:   -₹10,000
Outstanding:     ₹64,000

Payment History:
P001  Jan 20  ₹1,00,000  RTGS  UTR:HDFC2024012000123  Applied
CM001 Jan 25  ₹10,000    Credit Memo                   Applied

Status History:
Jan 15 09:00  Rahul   Draft
Jan 15 10:00  Priya   Approved
Jan 15 11:00  Rahul   Sent
Jan 20 14:00  System  Partially Paid
```

### Future Enhancements (Phase 2)
- E-invoicing compliance: India IRP/IRN mandate, EU Peppol network
- Full tax engine (CGST/SGST/IGST rules)
- Proforma invoice and advance payment
- Recurring invoices (SaaS/retainers)
- Invoice templates
- Bulk invoice generation

---

## FR2 — Invoice Approval

An invoice must be approved by an authorized person before it can be sent to the customer. Approval authority depends on invoice amount thresholds. System enforces that creator and approver must be different people (SOX compliance). On approval, GL journal entries are generated automatically. On rejection, invoice returns to Draft with mandatory rejection reason.

**Approval Thresholds:**
- Invoice < ₹1,00,000 → Accounts Officer can approve
- Invoice < ₹10,00,000 → Finance Manager can approve
- Invoice > ₹10,00,000 → CFO must approve

```
POST /invoices/{id}/approve

Invoice #1001 (from FR1)
Sender:   Reliance Retail, Mumbai (GSTIN: 27AAACR...)
Receiver: Tata Steel, Bangalore (GSTIN: 29AAACT...)

Step 1: Rahul (Accounts Officer) creates invoice
        Status: Draft

Step 2: Rahul submits for approval
        Amount ₹1,74,000 < ₹10,00,000
        → Auto-routed to Priya (Finance Manager)
        Status: Draft (is_submitted: true)

Step 3: Priya rejects ❌
        Reason: "Line 2 Safety Valves — interstate supply
        must use IGST not CGST+SGST"
        Status: Draft (is_submitted: false)

Step 4: Rahul fixes Line 2 tax jurisdiction
        MH → KA, CGST+SGST → IGST
        Resubmits to Priya
        Status: Draft (is_submitted: true)

Step 5: Priya approves ✅
        created_by:  Rahul (user_id: 123)
        approved_by: Priya (user_id: 456)
        created_by ≠ approved_by → SOX compliant ✅

Auto GL Entry Generated:
Debit:  1200 AR           ₹1,74,000  ← Tata Steel owes Reliance
Credit: 3100 Revenue      ₹1,50,000  ← Reliance earned it
Credit: 2200 Tax Payable    ₹24,000  ← collected for the government

Status: Draft → Approved ✅
```

### Future Enhancements (Phase 2)
- Approval SLA + escalation chain (needs Temporal)
- Delegation of authority (CFO on leave)
- Bulk approval

---

## FR3 — Invoice Sending

**Prototype extension implemented asynchronously after approval.**

Approval atomically inserts a PostgreSQL outbox event. A separate publisher
relays it to Standard SQS, and a consumer delivers it to the idempotent console
stub. Three failed receives redrive to a DLQ. Success records `sent_at` and
transitions APPROVED to SENT. Payment remains valid while delivery is pending
because approval—not notification—establishes the receivable. Production
replaces LocalStack and the stub with managed SQS plus Email/EDI/IRP adapters.

```
GET  /invoices/{id}/delivery       # inspect durable status and attempts
POST /delivery-events/{id}/retry   # CFO requeues a DEAD event

There is deliberately no synchronous /send command. Approval creates the
delivery fact automatically, and operational retry never repeats accounting.
```

### Future Enhancements (Phase 2)
- E-invoicing compliance: India IRP/IRN mandate, EU Peppol network, Italy mandate
- Multiple delivery methods: Email, Customer Portal, EDI
- Production DLQ alarms, retention and bulk redrive operations
- B2G compliance for US government contracts
- Invoice reminder emails (due in 7 days, overdue!)
- Customer portal

---

## FR4 — Payment Recording and Allocation

System must record payments from customers and allocate them against one or more outstanding invoices. Payment allocation can be automatic (FIFO — oldest invoice first) or manual (customer specifies allocation). System must handle partial payments, full payments, and overpayments. All payments generate GL journal entries automatically.

**Payment Terms on Invoice:**
- payment_terms → NET30/NET60/NET90
- due_date → invoice_date + payment_terms

**Duplicate Prevention:**
- Every write action uses one client-generated idempotency key across all retries.
- A completed retry returns the original cached HTTP status and response body.
- The same key with a different request payload is rejected.
- A unique `(tenant_id, customer_id, payment_reference)` constraint provides a
  second, race-safe defence against importing the same bank transaction twice.

```
POST /payments

Example 1 — Full Payment (Auto FIFO):
Tata Steel outstanding invoices:
Invoice #1001  ₹1,74,000  due Jan 31  (oldest)
Invoice #1002  ₹80,000    due Jan 31
Invoice #1003  ₹46,000    due Jan 31

Payment #P001 received: ₹3,00,000 (RTGS)
UTR: HDFC2024013100123
Mode: Auto (FIFO)

Allocation:
├── Invoice #1001 ₹1,74,000 → Fully Paid ✅
├── Invoice #1002 ₹80,000   → Fully Paid ✅
└── Invoice #1003 ₹46,000   → Fully Paid ✅

GL Entry:
Debit:  1100 Cash    ₹3,00,000  (ref: P001)
Credit: 1200 AR      ₹3,00,000  (ref: P001)

---

Example 2 — Partial Payment (Auto FIFO):
Payment #P002 received: ₹2,00,000
Allocation:
├── Invoice #1001 ₹1,74,000 → Fully Paid ✅
└── Invoice #1002 ₹26,000   → Partially Paid 🔄 (₹54,000 remaining)
Invoice #1003 → Untouched ❌

GL Entry:
Debit:  1100 Cash    ₹2,00,000  (ref: P002)
Credit: 1200 AR      ₹2,00,000  (ref: P002)

Invoice Status:
#1001 → Paid
#1002 → Partially Paid
#1003 → Sent (unchanged)

---

Example 3 — Manual Allocation:
Payment #P003 received: ₹2,00,000
Mode: Manual (remittance advice from Tata Steel)
Allocation:
├── Invoice #1001 ₹1,00,000
├── Invoice #1002 ₹60,000
└── Invoice #1003 ₹40,000

Invoice Status:
#1001 → Partially Paid (₹74,000 remaining)
#1002 → Partially Paid (₹20,000 remaining)
#1003 → Partially Paid (₹6,000 remaining)

---

Example 4 — Overpayment:
Payment #P004 received: ₹4,00,000
Total outstanding:      ₹3,00,000
Overpayment:            ₹1,00,000

GL Entry:
Debit:  1100 Cash             ₹4,00,000
Credit: 1200 AR               ₹3,00,000  ← clears all invoices
Credit: 2100 Customer Credit  ₹1,00,000  ← liability, we owe them

Options:
1. Refund to Tata Steel
2. Apply to future invoices
3. Hold as advance

---

Example 5 — Multi-currency Partial Payments:
Invoice #2001: $1,000 (rate Jan 1 = 83) = ₹83,000 base

Payment 1: Feb 1  $600  rate=80
Debit:  Cash         ₹48,000
Debit:  FX Loss      ₹1,800
Credit: AR           ₹49,800

Payment 2: Apr 1  $400  rate=95
Debit:  Cash         ₹38,000
Credit: AR           ₹33,200
Credit: FX Gain      ₹4,800

Net FX Gain: ₹3,000
```

### Future Enhancements (Phase 2)
- Installment payment schedules
- Automated payment gateway webhook reconciliation
- Early payment discount 2/10 NET30
- Payment gateway integration (Razorpay/Stripe)
- Automated bank reconciliation
- Cheque payment handling

---

## FR5 — Credit Memo

A credit memo is issued to correct an approved/sent/paid invoice. It cannot be raised against draft or void invoices. Credit memos have their own approval lifecycle. On approval, GL entries are automatically reversed.

**Implementation status:** Route implemented at
`POST /invoices/{id}/credit-memos`; the full B6 entity, FX, paid/partial,
concurrency, idempotency, direct-journal and reconciliation matrix passes.

**Reasons:**
- OVERCHARGE → wrong price
- RETURN → goods returned (partial or full)
- DUPLICATE → invoice raised twice
- CANCEL → order cancelled after delivery

**Allowed against:** Approved ✅ Sent ✅ Partially Paid ✅ Paid ✅

**Not allowed against:** Draft ❌ Void ❌ Written Off ❌

```
POST /invoices/{id}/credit-memos

Invoice #1001: ₹1,74,000 (Sent to Tata Steel)
Tata Steel returns 2 damaged Safety Valves

Credit Memo #CM001:
Against Invoice: #1001
Reason: RETURN
Lines: Safety Valves × 2 @ ₹5,000 = ₹10,000
Approved by: Priya (Finance Manager)

GL Entry:
Debit:  3100 Revenue   ₹10,000  ← un-earned
Credit: 1200 AR        ₹10,000  ← Tata Steel owes less

If the credited amount includes tax, reverse the corresponding Tax Payable
amount as a separate debit rather than treating tax as revenue.

The credit reduces AR only up to the invoice's outstanding balance. Any amount
already paid is credited to GL 2100 Customer Credit, creating a liability until
it is refunded or applied elsewhere. The invoice row is locked and cumulative
Revenue/Tax reversals are capped at the original posted amounts.

Net Tata Steel owes: ₹1,64,000

States: Draft → Approved → Applied
```

### Future Enhancements (Phase 2)
- Replacement invoice linking
- Automated refund trigger on full CM against paid invoice
- Credit memo templates

---

## FR6 — Write-off

A write-off is raised when a customer cannot pay (bankruptcy, absconding, bad debt). It removes the outstanding amount from AR and records it as Bad Debt Expense. Requires CFO approval due to revenue impact. Write-off applies only to outstanding balance — already paid amount is never reversed.

**Implementation status:** CFO-only route implemented at
`POST /invoices/{id}/writeoff`; its INR happy path and final reconciliation
pass, while extended acceptance/hardening remains pending.

**Allowed against:** Sent ✅ Partially Paid ✅ Approved ✅

**Not allowed against:** Draft ❌ Paid ❌ Void ❌

```
POST /invoices/{id}/writeoff

Example 1 — Full Write-off:
Invoice #1001: ₹1,74,000 (Sent to Tata Steel)
Tata Steel declares bankruptcy.

Write-off #WO001:
Against Invoice: #1001
Reason: BANKRUPTCY
Approved by: CFO Priya (always CFO)

GL Entry:
Debit:  4100 Bad Debt Expense  ₹1,74,000  ← loss
Credit: 1200 AR                ₹1,74,000  ← remove from AR

Invoice Status: Written Off

---

Example 2 — Partial Payment + Write-off:
Invoice:   ₹1,74,000
Paid:      ₹1,00,000  ← already received, stays
Write-off: ₹74,000    ← remaining balance only

GL Entry:
Debit:  4100 Bad Debt Expense  ₹74,000
Credit: 1200 AR                ₹74,000

Invoice Status: Written Off
```

### Future Enhancements (Phase 2)
- Bad debt provision (reserve before write-off)
- Write-off reversal if customer pays later (zombie payments!)
- Automated write-off suggestion (90+ days aging)

---

## FR7 — Invoice Void

An invoice is voided when it should never have been raised or was raised in error. Void is different from write-off — write-off is customer cannot pay, void is invoice itself was wrong. Cannot void a paid or partially paid invoice — use credit memo instead.

**Implementation status:** Void route implemented at
`POST /invoices/{id}/void`; DRAFT-without-GL is tested. Posted-reversal
acceptance remains pending, and automatic reissue remains Phase 2.

**Reasons:**
- DUPLICATE → same invoice raised twice
- WRONG_CUSTOMER → sent to wrong entity
- DATA_ERROR → wrong amounts, wrong items

**Allowed against:** Draft ✅ Approved ✅ Sent ✅

**Not allowed against:** Paid ❌ Partially Paid ❌ Written Off ❌

```
POST /invoices/{id}/void

Invoice #1002 accidentally raised twice (duplicate of #1001)

Void #V001:
Against Invoice: #1002
Reason: DUPLICATE of #1001
Approved by: Finance Manager

GL Entry (if already approved):
Reverse original entry:
Debit:  3100 Revenue      ₹1,50,000  ← reverse revenue
Debit:  2200 Tax Payable    ₹24,000  ← reverse tax liability
Credit: 1200 AR           ₹1,74,000  ← remove receivable

Invoice Status: Void ✅
```

### Future Enhancements (Phase 2)
- Void + reissue workflow (void old, create corrected new invoice)
- Supplementary invoice (for undercharging scenarios)

---

## FR8 — AR Aging Report

System must generate AR aging report showing outstanding invoice balances grouped by days overdue. Used by CFO and collections team to track overdue payments and identify write-off candidates.

The prototype reports the current aging snapshot. Its `as_of` value identifies
when the materialized view was refreshed; clients cannot request an arbitrary
historical date.

**Aging Buckets:**
- Current → not yet due (due_date >= today)
- 30 days → overdue 1-30 days
- 60 days → overdue 31-60 days
- 90+ days → overdue 61+ days → danger zone!

**Calculation:**
- days_overdue = today - due_date
- Bucket assigned based on days_overdue
- Amount = open `base_balance_amount` in the entity's base currency
- Include only APPROVED, SENT and PARTIALLY_PAID invoices; DRAFT has not posted AR

```
GET /customers/{id}/aging

Customer: Tata Steel
As of: Jan 31, 2024

Bucket    | Invoices  | Amount
----------|-----------|----------
Current   | #1003     | ₹80,000
30 days   | #1002     | ₹74,000
60 days   | -         | ₹0
90+ days  | -         | ₹0
----------|-----------|----------
Total     |           | ₹1,54,000

Invoice detail:
#1002  due Jan 1   overdue 30 days  ₹74,000  → call collections
#1003  due Feb 1   current          ₹80,000  → monitor
```

### Future Enhancements (Phase 2)
- Full company aging report (all customers)
- Automated collections trigger at 90+ days
- Aging report by entity/subsidiary
- Aging report export (PDF/Excel)
- Aging trends (this month vs last month)
- Historical `as_of` reporting using dated financial events or daily snapshots

---

## FR9 — GL Journal Entries and Reconciliation

The system maintains a complete journal-entry trail for every implemented
financial event. API6 exposes the entries with cursor pagination, and the
health reconciliation check compares the AR subledger with the AR control
account. A scheduled reconciliation and alerting job is future hardening.

```
GET /journal-entries?invoice=1001

Invoice #1001 — Complete Financial History:

Date       | Entry | Type             | Debit               | Credit
-----------|-------|------------------|---------------------|--------------------
Jan 15     | JE001 | Invoice Approved | 1200 AR ₹1,74,000  | 3100 Rev ₹1,50,000 + 2200 Tax Payable ₹24,000
Jan 20     | JE002 | Payment Received | 1100 Cash ₹1,00,000 | 1200 AR ₹1,00,000
Jan 25     | JE003 | Credit Memo      | 3100 Rev ₹10,000   | 1200 AR ₹10,000
-----------|-------|------------------|---------------------|--------------------
Net AR outstanding: ₹64,000
```

**Reconciliation Check:**
```
Reconciliation Job — Jan 31, 2024 02:00am

GL Account 1200 (AR):
Sum of all journal entry lines = ₹2,34,000

AR Subledger:
Invoice #1001  Tata Steel   ₹64,000    (partially paid)
Invoice #1002  JSW Steel    ₹1,40,000  (open)
Invoice #1003  Adani Ports  ₹30,000    (open)
                            ----------
Total:                      ₹2,34,000

GL = Subledger? ✅ Books are clean!

Mismatch scenario:
GL shows:        ₹2,34,000
Subledger shows: ₹2,44,000
Difference:      ₹10,000 ❌ ALERT! Finance team notified immediately
```

**Database Transaction Guarantee:**
```
BEGIN TRANSACTION
  INSERT INTO invoices...
  INSERT INTO journal_entries...
COMMIT
← both succeed or both fail. never partial!
```

**Journal Entry Rules:**
- Application code treats posted entries as append-only
- Always balanced — debits = credits
- Always referenced — every JE links to source document
- Long-term immutable retention is production hardening

### Future Enhancements (Phase 2)
- Scheduled reconciliation, alerting and operational ownership
- Database privilege hardening to prohibit journal update/delete
- Automated mismatch correction
- Kafka → S3 WORM archival for 7 year retention
- Reconciliation dashboard

---

## FR10 — Multi-tenant Isolation

System must completely isolate data between tenants. No tenant can ever see another tenant's data. Isolation enforced at both application layer (JWT) and database layer (Row Level Security). Every table contains tenant_id. Every query filters by tenant_id.

**Tenant Hierarchy:**
```
Tenant (Reliance)                ← one contract, one JWT
├── Entity (Reliance Retail)     ← entity_id in JWT
├── Entity (Reliance Jio)
└── Entity (Reliance Industries)
```

**JWT Token:**
```json
{
  "user_id": "rahul-123",
  "tenant_id": "reliance",
  "entity_id": "reliance-retail",
  "roles": ["invoice_creator"]
}
```

**App Layer:**
```
Every API request:
1. Extract tenant_id from JWT
2. Inject into every query automatically
3. Never trust tenant_id from request body!

GET /invoices
→ SELECT * FROM invoices
  WHERE tenant_id = 'reliance'        ← from JWT
  AND entity_id = 'reliance-retail'   ← from JWT
```

**Database Layer (Row Level Security):**
```sql
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON invoices
  USING (tenant_id = current_setting('app.tenant_id'));
```

**IoT Parallel (Lenovo project):**
```
IoT:                    ERP:
Customer                Tenant
└── Site                └── Entity
    └── Device              └── Invoice/Payment

IoT: WHERE customer_id=X
ERP: WHERE tenant_id=X AND entity_id=Y
```

### Future Enhancements (Phase 2)
- Cross-tenant reporting for SaaS vendor
- Tenant onboarding automation
- Tenant-level feature flags
- Tenant data export (GDPR right to data)
- Tenant offboarding (contract ends)

---

## FR11 — Multi-currency

**Implementation status:** `COMPLETE` for V1 — ingestion, invoice/payment rate
snapshots, base-currency journals, realized gain/loss, partial payments and
fail-closed tests are implemented. See
[FX Rate Ingestion and Multi-Currency Design](fx-rate-design.md).

V1 supports INR, USD, EUR, CNY, GBP, JPY, CHF and CAD. Exchange rates are
fetched after the ECB weekday publication and stored in PostgreSQL. An invoice
stores both transaction-currency and entity-base-currency amounts. Realized FX
gain/loss is calculated when the payment-date rate differs from the locked
invoice-date rate. V1 requires the payment and invoice to use the same
transaction currency; cross-currency settlement is V2.

**Exchange Rate Storage:**
```
pg_cron (21:00 IST weekdays):
Insert one PENDING fx_import_job
FX worker fetches official ECB EUR reference quotes
Worker derives and stores tenant-approved foreign→INR rates:

from_currency:  USD
to_currency:    INR
rate:           83.00
effective_date: 2024-01-01
source:         ECB_DAILY_REFERENCE
```

**Invoice Amounts (both stored):**
```
Invoice #2001
Reliance USA → US Customer

transaction_currency: USD
transaction_amount:   $1,000    ← sent to customer
exchange_rate:        83.00     ← locked on invoice date
base_currency:        INR
base_amount:          ₹83,000   ← for CFO reporting
```

**Payment with FX Gain/Loss:**
```
Invoice date:  Jan 1   rate=83  $1,000 = ₹83,000
Payment date:  Mar 1   rate=86  $1,000 = ₹86,000
FX Gain = ₹3,000

GL Entry on payment:
Debit:  1100 Cash          ₹86,000  ← actual cash received
Credit: 1200 AR            ₹83,000  ← original invoice amount
Credit: 4300 FX Gain/Loss  ₹3,000   ← difference
```

**CFO Consolidated Report:**
```
Reliance Group AR — Jan 31, 2024

Entity                | Currency | Amount      | INR Equivalent
----------------------|----------|-------------|----------------
Reliance Retail (IN)  | INR      | ₹1,74,000   | ₹1,74,000
Reliance USA          | USD      | $1,000      | ₹83,000
Reliance Sri Lanka    | LKR      | LKR 50,000  | ₹10,250
----------------------|----------|-------------|----------------
Total (INR)           |          |             | ₹2,67,250

FX Summary:
FX Loss:  -₹1,800
FX Gain:  +₹4,800
Net:      +₹3,000
```

Missing Exchange Rate Handling:
If rate not found for invoice date:
→ Use latest APPROVED prior-business-day rate only when <= 3 days old
→ Disclose exact rate ID, provider date and prior-date warning
→ If missing/older than 3 days, reject with FX_RATE_UNAVAILABLE
→ Never substitute 1.0 for a foreign-currency pair

Example:
Invoice date: Jan 1 (Sunday, markets closed, no rate available)
System uses:  Dec 31 rate (most recent available)
Response:     "Prior-business-day rate from Dec 31 used"
Audit trail:  Records immutable rate ID, value, source date and reason

### Future Enhancements (Phase 2)
- Multiple exchange rate types (spot/forward/average)
- Hedging support
- Unrealized currency revaluation and reversal at period end
- Cross-currency settlement (for example EUR payment against USD invoice)
- Manual rate-entry and CFO-approval API; immutable supersession is already part
  of the V1 data design

---

## FR12 — Audit Trail

Financial record changes are captured in PostgreSQL `audit_log` by database
triggers, including actor, action, timestamp, and old/new values. This is the
implemented V1 queryable audit trail. Restrictive database privileges and
seven-year WORM archival remain production hardening.

```
GET /audit-log?record_type=invoice&record_id=1001

Invoice #1001 — Audit Trail:

Time                | User  | Action | Field         | Old      | New
--------------------|-------|--------|---------------|----------|-------------
2024-01-15 09:00   | Rahul | CREATE | invoice       | null     | {draft}
2024-01-15 10:00   | Priya | UPDATE | status        | draft    | approved
2024-01-15 10:00   | Priya | INSERT | journal_entry | null     | JE001
2024-01-15 11:00   | Rahul | UPDATE | status        | approved | sent
2024-01-20 14:00   | Priya | UPDATE | status        | sent     | partially_paid
2024-01-25 09:00   | Rahul | INSERT | credit_memo   | null     | CM001
```

**Storage Tiering:**
```
Audit table (PostgreSQL) → implemented; quick audit queries
Kafka/event stream       → future; replay and archival pipeline
S3 Object Lock/WORM      → future; seven-year immutable retention
```

### Future Enhancements (Phase 2)
- Restrict application roles from updating or deleting audit rows
- Seven-year immutable archival and retention enforcement
- Real time audit dashboard
- Anomaly detection (unusual changes flagged)
- Full event sourcing
- Audit report export for SOX auditors
- User session tracking (IP, device, location)

---

## FR13 — Accounting Period Enforcement

The implemented scope prevents posting to non-OPEN monthly/yearly periods at
both application and database-trigger levels. Two levels of closure are
modelled: CLOSED (eligible for a future audited CFO reopen workflow) and LOCKED
(permanent after audit/tax filing).

```
Accounting Periods — Reliance Retail:

Period    | Start      | End        | Status | Closed By | Closed At
----------|------------|------------|--------|-----------|------------------
Jan 2024  | 2024-01-01 | 2024-01-31 | LOCKED | CFO Priya | 2024-03-01
Feb 2024  | 2024-02-01 | 2024-02-28 | CLOSED | CFO Priya | 2024-03-05
Mar 2024  | 2024-03-01 | 2024-03-31 | OPEN   | -         | -

Rahul tries to approve invoice dated Jan 28 on Feb 5:
❌ "January 2024 is LOCKED. Cannot post.
    Use a CFO-approved current-period adjustment."
```

**Two Lock Levels:**
```
CLOSED → CFO can reopen (forgot one invoice etc)
LOCKED → permanent. tax filed. auditor signed off.
         nobody can reopen. not even CFO!
```

**Enforcement:**
```python
# App level
if period.status in ['CLOSED', 'LOCKED']:
    raise PeriodClosedException()

# DB level trigger on journal_entries
BEFORE INSERT → verify period is OPEN
```

**Prior Period Corrections:**
```
January LOCKED but error found?
→ Cannot reopen January
→ Post correction in current open period (March)
→ Preserve document_date = original January date
→ Set entry_date (posting date) = date in current open period
→ With clear reference to original January entry
→ Auditor sees full correction trail
```

Controls: CFO approval, mandatory reason, reference to the original invoice and
locked period, creator != approver, and an immutable audit trail. New journal
entries are never backdated into a LOCKED period.

---

## FR14 — RBAC and Segregation of Duties

Role-based authorization is implemented for financial APIs. JWT roles determine
allowed actions, and SOX segregation prevents the same user from creating and
approving an invoice.

**Roles:**
```
invoice_creator   → create/edit draft invoices
invoice_approver  → approve/reject invoices
payment_recorder  → record payments
cfo               → write-offs and financial administration
auditor           → read-only access to financial APIs
```

**Example:**
```
Rahul → role: invoice_creator
Priya → role: invoice_approver, cfo

POST /invoices/{id}/approve
Request by: Rahul (invoice_creator)
Response: 403 Forbidden

Request by: Priya (invoice_approver)
Response: 200 OK

SOX segregation:
created_by != approved_by → enforced in code and DB
```

---

## Future-FR1 — Intercompany Consolidation

**Why deferred:** The assessment's core AR flow ends at the seller entity;
buyer-side AP posting, matching and consolidation require a separate
cross-entity accounting workflow. **Plan:** Yes — targeted for V2 using an
audited consolidation ledger without rewriting statutory entity books.

Multi-entity scoping is implemented under FR10. Intercompany buyer-side
posting, matching, elimination and consolidated reporting are a future
capability. Each entity must retain its own statutory books.

```
GET /reports/consolidated?tenant_id=T001&as_of=2024-01-31
GET /reports/entity?entity_id=E002&as_of=2024-01-31

Reliance Retail (E002) invoices Reliance Jio (E003)
₹10,00,000 for IT services

Invoice #3001:
tenant_id:          T001
sender_entity_id:   E002  ← Reliance Retail
receiver_entity_id: E003  ← Reliance Jio
amount:             ₹10,00,000
is_intercompany:    TRUE  ← auto detected!

Reliance Retail books (E002):
Debit:  AR         ₹10,00,000
Credit: Revenue    ₹10,00,000

Reliance Jio books (E003):
Debit:  IT Expense ₹10,00,000
Credit: AP         ₹10,00,000

Consolidated Report (T001):
                 Retail        Jio          Elimination   Consolidated
Revenue:         ₹10,00,000   ₹0           -₹10,00,000   ₹0
Expense:         ₹0           ₹10,00,000   -₹10,00,000   ₹0
AR:              ₹10,00,000   ₹0           -₹10,00,000   ₹0
AP:              ₹0           ₹10,00,000   -₹10,00,000   ₹0
```

The seller and buyer journals remain unchanged in their legal-entity ledgers.
V2 posts the four elimination lines to a separate consolidation ledger, linked
to the matched intercompany transaction and reporting period. Consolidated
reporting combines the entity ledgers with this elimination ledger; it never
rewrites either entity's statutory books.

**Intercompany Detection:**
```
IF sender_entity_id AND receiver_entity_id
   both have same tenant_id
→ is_intercompany = TRUE
→ eliminate in consolidated report
```

**External Customer (not intercompany):**
```
Reliance Retail → Tata Steel
tenant_id:         T001
sender_entity_id:  E002
customer_id:       tata-steel  ← external customer!
is_intercompany:   FALSE ✅
```

### Future Enhancements (Phase 2)
- Intercompany reconciliation and matching (Retail AR = Jio AP?)
- Audited, idempotent elimination batches in a separate consolidation ledger
- Mismatch workflow instead of silently eliminating unmatched balances
- Transfer pricing compliance
- Minority interest handling
- Intercompany netting (offset AR vs AP between entities)
- Consolidated report export

---

## Future-FR2 — Manual Journal Entry

**Why deferred:** Manual posting can bypass source-document controls and needs
a dedicated maker-checker workflow, approval limits and reversal handling;
these were outside the prototype's required APIs. **Plan:** Yes — targeted for
V2 after those controls and period-management APIs exist.

System must support manual journal entries for corrections, prior period adjustments, and write-off corrections. Manual entries require CFO approval and mandatory description. Full audit trail captured. Cannot be posted to locked periods.

```
POST /journal-entries/manual
GET  /journal-entries/manual?status=pending
POST /journal-entries/manual/{id}/approve

Scenario: Reconciliation found ₹10,000 mismatch
          GL shows ₹2,44,000, Subledger shows ₹2,34,000

Manual Journal Entry #MJE001:
Created by:  Rahul (Accounts Officer)
Approved by: CFO Priya
Period:      February 2024 (OPEN)
Description: "Correction for duplicate AR entry
              found during Jan reconciliation"

Entry:
Debit:  1200 AR        ₹10,000
Credit: 3100 Revenue   ₹10,000

Audit Trail:
Created:  Rahul  Feb 5 10:00
Approved: Priya  Feb 5 11:00
Posted:   System Feb 5 11:00
```

**Rules:**
- CFO approval mandatory (always!)
- Description mandatory
- Original document date and adjusted period reference mandatory for prior-period corrections
- Creator and approver must be different users (SOX segregation of duties)
- Cannot post to LOCKED period
- Cannot post to CLOSED period without CFO reopening
- Immutable once posted
- Full audit trail

### Future Enhancements (Phase 2)
- Recurring journal entries (depreciation etc)
- Reversing journal entries (auto reverse next period)

---

## Future-FR3 — Period Management APIs

**Why deferred:** Posting into non-OPEN periods is already prevented, while
closing, locking and reopening periods are administrative workflows requiring
CFO authorization, mandatory reasons and stronger audit tests. **Plan:** Yes —
targeted for V2; enforcement remains active in V1.

The posting enforcement exists; administrative workflow endpoints are
deliberately deferred:

```text
POST /periods/{id}/close    ← CFO closes period
POST /periods/{id}/lock     ← permanent lock after audit
POST /periods/{id}/reopen   ← CFO reopens CLOSED with an audited reason
GET  /periods               ← list periods and status
```

- Automated period-close checklist and multiple sign-offs
- Soft close versus hard close
- Period-close exception report
- Automatic creation/opening of the next period

---

## Future-FR4 — User Management APIs

**Why deferred:** V1 consumes signed JWT identities and seeded roles; secure
user provisioning, credential lifecycle and role administration belong behind
an enterprise identity provider rather than the AR service itself. **Plan:**
Yes — V2 will integrate an IdP and expose only the required ERP role-mapping
administration.

Role administration is not part of the implemented prototype:

```
POST /users                    ← create user
POST /users/{id}/roles         ← assign role
DELETE /users/{id}/roles/{role}← revoke role
GET  /users                    ← list users
```

### Future Enhancements (Phase 2)
- Entity level permissions (approve invoices for Retail but not Jio)
- Role hierarchies
- Permission inheritance
- Temporary role delegation (CFO on leave)
