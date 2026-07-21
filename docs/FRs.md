# Functional Requirements ‚Ä" ERP AR Module

## Table of Contents
- [FR1 ‚Ä" Invoice Creation](#fr1--invoice-creation)
- [FR2 ‚Ä" Invoice Approval](#fr2--invoice-approval)
- [FR3 ‚Ä" Invoice Sending](#fr3--invoice-sending)
- [FR4 ‚Ä" Payment Recording and Allocation](#fr4--payment-recording-and-allocation)
- [FR5 ‚Ä" Credit Memo](#fr5--credit-memo)
- [FR6 ‚Ä" Write-off](#fr6--write-off)
- [FR7 ‚Ä" Invoice Void](#fr7--invoice-void)
- [FR8 ‚Ä" AR Aging Report](#fr8--ar-aging-report)
- [FR9 ‚Ä" GL Journal Entries and Reconciliation](#fr9--gl-journal-entries-and-reconciliation)
- [FR10 ‚Ä" Multi-tenant Isolation](#fr10--multi-tenant-isolation)
- [FR11 ‚Ä" Multi-currency](#fr11--multi-currency)
- [FR12 ‚Ä" Audit Trail](#fr12--audit-trail)
- [FR13 ‚Ä" Accounting Period Enforcement](#fr13--accounting-period-enforcement)
- [FR14 ‚Ä" RBAC and Segregation of Duties](#fr14--rbac-and-segregation-of-duties)
- [Future-FR1 ‚Ä" Intercompany Consolidation](#future-fr1--intercompany-consolidation)
- [Future-FR2 ‚Ä" Manual Journal Entry](#future-fr2--manual-journal-entry)
- [Future-FR3 ‚Ä" Period Management APIs](#future-fr3--period-management-apis)
- [Future-FR4 ‚Ä" User Management APIs](#future-fr4--user-management-apis)

---

## FR1 ‚Ä" Invoice Creation

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
| Industrial Pump  |  2  | ‚Çπ50,000  | 18%  | MH     | ‚Çπ1,18,000 |
| Safety Valves    | 10  | ‚Çπ5,000   | 12%  | KA     | ‚Çπ56,000   |

Subtotal:  ‚Çπ1,50,000
Tax:       ‚Çπ24,000
Total:     ‚Çπ1,74,000
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
| Industrial Pump  |  2  | ‚Çπ50,000  | 18%  | MH     | ‚Çπ1,18,000 |
| Safety Valves    | 10  | ‚Çπ5,000   | 12%  | KA     | ‚Çπ56,000   |

Subtotal:        ‚Çπ1,50,000
Tax:             ‚Çπ24,000
Invoice Total:   ‚Çπ1,74,000
Paid:            ‚Çπ1,00,000
Credit Memos:   -‚Çπ10,000
Outstanding:     ‚Çπ64,000

Payment History:
P001  Jan 20  ‚Çπ1,00,000  RTGS  UTR:HDFC2024012000123  Applied
CM001 Jan 25  ‚Çπ10,000    Credit Memo                   Applied

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

## FR2 ‚Ä" Invoice Approval

An invoice must be approved by an authorized person before it can be sent to the customer. Approval authority depends on invoice amount thresholds. System enforces that creator and approver must be different people (SOX compliance). On approval, GL journal entries are generated automatically. On rejection, invoice returns to Draft with mandatory rejection reason.

**Approval Thresholds:**
- Invoice < ‚Çπ1,00,000 ‚Ü' Accounts Officer can approve
- Invoice < ‚Çπ10,00,000 ‚Ü' Finance Manager can approve
- Invoice > ‚Çπ10,00,000 ‚Ü' CFO must approve

```
POST /invoices/{id}/approve

Invoice #1001 (from FR1)
Sender:   Reliance Retail, Mumbai (GSTIN: 27AAACR...)
Receiver: Tata Steel, Bangalore (GSTIN: 29AAACT...)

Step 1: Rahul (Accounts Officer) creates invoice
        Status: Draft

Step 2: Rahul submits for approval
        Amount ‚Çπ1,74,000 < ‚Çπ10,00,000
        ‚Ü' Auto-routed to Priya (Finance Manager)
        Status: Draft (is_submitted: true)

Step 3: Priya rejects ‚ùå
        Reason: "Line 2 Safety Valves ‚Ä" interstate supply
        must use IGST not CGST+SGST"
        Status: Draft (is_submitted: false)

Step 4: Rahul fixes Line 2 tax jurisdiction
        MH ‚Ü' KA, CGST+SGST ‚Ü' IGST
        Resubmits to Priya
        Status: Draft (is_submitted: true)

Step 5: Priya approves ‚úÖ
        created_by:  Rahul (user_id: 123)
        approved_by: Priya (user_id: 456)
        created_by ‚â† approved_by ‚Ü' SOX compliant ‚úÖ

Auto GL Entry Generated:
Debit:  1200 AR           ‚Çπ1,74,000  ‚Üê Tata Steel owes Reliance
Credit: 3100 Revenue      ‚Çπ1,50,000  ‚Üê Reliance earned it
Credit: 2200 Tax Payable    ‚Çπ24,000  ‚Üê collected for the government

Status: Draft ‚Ü' Approved ‚úÖ
```

### Future Enhancements (Phase 2)
- Approval SLA + escalation chain (needs Temporal)
- Delegation of authority (CFO on leave)
- Bulk approval

---

## FR3 ‚Ä" Invoice Sending

**Prototype extension implemented asynchronously after approval.**

Approval atomically inserts a PostgreSQL outbox event. A separate publisher
relays it to Standard SQS, and a consumer delivers it to the idempotent console
stub. Three failed receives redrive to a DLQ. Success records `sent_at` and
transitions APPROVED to SENT. Payment remains valid while delivery is pending
because approval‚Ä"not notification‚Ä"establishes the receivable. Production
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

## FR4 ‚Ä" Payment Recording and Allocation

System must record payments from customers and allocate them against one or more outstanding invoices. Payment allocation can be automatic (FIFO ‚Ä" oldest invoice first) or manual (customer specifies allocation). System must handle partial payments, full payments, and overpayments. All payments generate GL journal entries automatically.

**Payment Terms on Invoice:**
- payment_terms ‚Ü' NET30/NET60/NET90
- due_date ‚Ü' invoice_date + payment_terms

**Duplicate Prevention:**
- Every write action uses one client-generated idempotency key across all retries.
- A completed retry returns the original cached HTTP status and response body.
- The same key with a different request payload is rejected.
- A unique `(tenant_id, customer_id, payment_reference)` constraint provides a
  second, race-safe defence against importing the same bank transaction twice.

```
POST /payments

Example 1 ‚Ä" Full Payment (Auto FIFO):
Tata Steel outstanding invoices:
Invoice #1001  ‚Çπ1,74,000  due Jan 31  (oldest)
Invoice #1002  ‚Çπ80,000    due Jan 31
Invoice #1003  ‚Çπ46,000    due Jan 31

Payment #P001 received: ‚Çπ3,00,000 (RTGS)
UTR: HDFC2024013100123
Mode: Auto (FIFO)

Allocation:
‚"ú‚"Ä‚"Ä Invoice #1001 ‚Çπ1,74,000 ‚Ü' Fully Paid ‚úÖ
‚"ú‚"Ä‚"Ä Invoice #1002 ‚Çπ80,000   ‚Ü' Fully Paid ‚úÖ
‚""‚"Ä‚"Ä Invoice #1003 ‚Çπ46,000   ‚Ü' Fully Paid ‚úÖ

GL Entry:
Debit:  1100 Cash    ‚Çπ3,00,000  (ref: P001)
Credit: 1200 AR      ‚Çπ3,00,000  (ref: P001)

---

Example 2 ‚Ä" Partial Payment (Auto FIFO):
Payment #P002 received: ‚Çπ2,00,000
Allocation:
‚"ú‚"Ä‚"Ä Invoice #1001 ‚Çπ1,74,000 ‚Ü' Fully Paid ‚úÖ
‚""‚"Ä‚"Ä Invoice #1002 ‚Çπ26,000   ‚Ü' Partially Paid ü"Ñ (‚Çπ54,000 remaining)
Invoice #1003 ‚Ü' Untouched ‚ùå

GL Entry:
Debit:  1100 Cash    ‚Çπ2,00,000  (ref: P002)
Credit: 1200 AR      ‚Çπ2,00,000  (ref: P002)

Invoice Status:
#1001 ‚Ü' Paid
#1002 ‚Ü' Partially Paid
#1003 ‚Ü' Sent (unchanged)

---

Example 3 ‚Ä" Manual Allocation:
Payment #P003 received: ‚Çπ2,00,000
Mode: Manual (remittance advice from Tata Steel)
Allocation:
‚"ú‚"Ä‚"Ä Invoice #1001 ‚Çπ1,00,000
‚"ú‚"Ä‚"Ä Invoice #1002 ‚Çπ60,000
‚""‚"Ä‚"Ä Invoice #1003 ‚Çπ40,000

Invoice Status:
#1001 ‚Ü' Partially Paid (‚Çπ74,000 remaining)
#1002 ‚Ü' Partially Paid (‚Çπ20,000 remaining)
#1003 ‚Ü' Partially Paid (‚Çπ6,000 remaining)

---

Example 4 ‚Ä" Overpayment:
Payment #P004 received: ‚Çπ4,00,000
Total outstanding:      ‚Çπ3,00,000
Overpayment:            ‚Çπ1,00,000

GL Entry:
Debit:  1100 Cash             ‚Çπ4,00,000
Credit: 1200 AR               ‚Çπ3,00,000  ‚Üê clears all invoices
Credit: 2100 Customer Credit  ‚Çπ1,00,000  ‚Üê liability, we owe them

Options:
1. Refund to Tata Steel
2. Apply to future invoices
3. Hold as advance

---

Example 5 ‚Ä" Multi-currency Partial Payments:
Invoice #2001: $1,000 (rate Jan 1 = 83) = ‚Çπ83,000 base

Payment 1: Feb 1  $600  rate=80
Debit:  Cash         ‚Çπ48,000
Debit:  FX Loss      ‚Çπ1,800
Credit: AR           ‚Çπ49,800

Payment 2: Apr 1  $400  rate=95
Debit:  Cash         ‚Çπ38,000
Credit: AR           ‚Çπ33,200
Credit: FX Gain      ‚Çπ4,800

Net FX Gain: ‚Çπ3,000
```

### Future Enhancements (Phase 2)
- Installment payment schedules
- Automated payment gateway webhook reconciliation
- Early payment discount 2/10 NET30
- Payment gateway integration (Razorpay/Stripe)
- Automated bank reconciliation
- Cheque payment handling

---

## FR5 ‚Ä" Credit Memo

A credit memo is issued to correct an approved/sent/paid invoice. It cannot be raised against draft or void invoices. Credit memos have their own approval lifecycle. On approval, GL entries are automatically reversed.

**Implementation status:** Route implemented at
`POST /invoices/{id}/credit-memos`; the full B6 entity, FX, paid/partial,
concurrency, idempotency, direct-journal and reconciliation matrix passes.

**Reasons:**
- OVERCHARGE ‚Ü' wrong price
- RETURN ‚Ü' goods returned (partial or full)
- DUPLICATE ‚Ü' invoice raised twice
- CANCEL ‚Ü' order cancelled after delivery

**Allowed against:** Approved ‚úÖ Sent ‚úÖ Partially Paid ‚úÖ Paid ‚úÖ

**Not allowed against:** Draft ‚ùå Void ‚ùå Written Off ‚ùå

```
POST /invoices/{id}/credit-memos

Invoice #1001: ‚Çπ1,74,000 (Sent to Tata Steel)
Tata Steel returns 2 damaged Safety Valves

Credit Memo #CM001:
Against Invoice: #1001
Reason: RETURN
Lines: Safety Valves √-- 2 @ ‚Çπ5,000 = ‚Çπ10,000
Approved by: Priya (Finance Manager)

GL Entry:
Debit:  3100 Revenue   ‚Çπ10,000  ‚Üê un-earned
Credit: 1200 AR        ‚Çπ10,000  ‚Üê Tata Steel owes less

If the credited amount includes tax, reverse the corresponding Tax Payable
amount as a separate debit rather than treating tax as revenue.

The credit reduces AR only up to the invoice's outstanding balance. Any amount
already paid is credited to GL 2100 Customer Credit, creating a liability until
it is refunded or applied elsewhere. The invoice row is locked and cumulative
Revenue/Tax reversals are capped at the original posted amounts.

Net Tata Steel owes: ‚Çπ1,64,000

States: Draft ‚Ü' Approved ‚Ü' Applied
```

### Future Enhancements (Phase 2)
- Replacement invoice linking
- Automated refund trigger on full CM against paid invoice
- Credit memo templates

---

## FR6 ‚Ä" Write-off

A write-off is raised when a customer cannot pay (bankruptcy, absconding, bad debt). It removes the outstanding amount from AR and records it as Bad Debt Expense. Requires CFO approval due to revenue impact. Write-off applies only to outstanding balance ‚Ä" already paid amount is never reversed.

**Implementation status:** CFO-only route implemented at
`POST /invoices/{id}/writeoff`; its INR happy path and final reconciliation
pass, while extended acceptance/hardening remains pending.

**Allowed against:** Sent ‚úÖ Partially Paid ‚úÖ Approved ‚úÖ

**Not allowed against:** Draft ‚ùå Paid ‚ùå Void ‚ùå

```
POST /invoices/{id}/writeoff

Example 1 ‚Ä" Full Write-off:
Invoice #1001: ‚Çπ1,74,000 (Sent to Tata Steel)
Tata Steel declares bankruptcy.

Write-off #WO001:
Against Invoice: #1001
Reason: BANKRUPTCY
Approved by: CFO Priya (always CFO)

GL Entry:
Debit:  4100 Bad Debt Expense  ‚Çπ1,74,000  ‚Üê loss
Credit: 1200 AR                ‚Çπ1,74,000  ‚Üê remove from AR

Invoice Status: Written Off

---

Example 2 ‚Ä" Partial Payment + Write-off:
Invoice:   ‚Çπ1,74,000
Paid:      ‚Çπ1,00,000  ‚Üê already received, stays
Write-off: ‚Çπ74,000    ‚Üê remaining balance only

GL Entry:
Debit:  4100 Bad Debt Expense  ‚Çπ74,000
Credit: 1200 AR                ‚Çπ74,000

Invoice Status: Written Off
```

### Future Enhancements (Phase 2)
- Bad debt provision (reserve before write-off)
- Write-off reversal if customer pays later (zombie payments!)
- Automated write-off suggestion (90+ days aging)

---

## FR7 ‚Ä" Invoice Void

An invoice is voided when it should never have been raised or was raised in error. Void is different from write-off ‚Ä" write-off is customer cannot pay, void is invoice itself was wrong. Cannot void a paid or partially paid invoice ‚Ä" use credit memo instead.

**Implementation status:** Void route implemented at
`POST /invoices/{id}/void`; DRAFT-without-GL is tested. Posted-reversal
acceptance remains pending, and automatic reissue remains Phase 2.

**Reasons:**
- DUPLICATE ‚Ü' same invoice raised twice
- WRONG_CUSTOMER ‚Ü' sent to wrong entity
- DATA_ERROR ‚Ü' wrong amounts, wrong items

**Allowed against:** Draft ‚úÖ Approved ‚úÖ Sent ‚úÖ

**Not allowed against:** Paid ‚ùå Partially Paid ‚ùå Written Off ‚ùå

```
POST /invoices/{id}/void

Invoice #1002 accidentally raised twice (duplicate of #1001)

Void #V001:
Against Invoice: #1002
Reason: DUPLICATE of #1001
Approved by: Finance Manager

GL Entry (if already approved):
Reverse original entry:
Debit:  3100 Revenue      ‚Çπ1,50,000  ‚Üê reverse revenue
Debit:  2200 Tax Payable    ‚Çπ24,000  ‚Üê reverse tax liability
Credit: 1200 AR           ‚Çπ1,74,000  ‚Üê remove receivable

Invoice Status: Void ‚úÖ
```

### Future Enhancements (Phase 2)
- Void + reissue workflow (void old, create corrected new invoice)
- Supplementary invoice (for undercharging scenarios)

---

## FR8 ‚Ä" AR Aging Report

System must generate AR aging report showing outstanding invoice balances grouped by days overdue. Used by CFO and collections team to track overdue payments and identify write-off candidates.

The prototype reports the current aging snapshot. Its `as_of` value identifies
when the materialized view was refreshed; clients cannot request an arbitrary
historical date.

**Aging Buckets:**
- Current ‚Ü' not yet due (due_date >= today)
- 30 days ‚Ü' overdue 1-30 days
- 60 days ‚Ü' overdue 31-60 days
- 90+ days ‚Ü' overdue 61+ days ‚Ü' danger zone!

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
Current   | #1003     | ‚Çπ80,000
30 days   | #1002     | ‚Çπ74,000
60 days   | -         | ‚Çπ0
90+ days  | -         | ‚Çπ0
----------|-----------|----------
Total     |           | ‚Çπ1,54,000

Invoice detail:
#1002  due Jan 1   overdue 30 days  ‚Çπ74,000  ‚Ü' call collections
#1003  due Feb 1   current          ‚Çπ80,000  ‚Ü' monitor
```

### Future Enhancements (Phase 2)
- Full company aging report (all customers)
- Automated collections trigger at 90+ days
- Aging report by entity/subsidiary
- Aging report export (PDF/Excel)
- Aging trends (this month vs last month)
- Historical `as_of` reporting using dated financial events or daily snapshots

---

## FR9 ‚Ä" GL Journal Entries and Reconciliation

The system maintains a complete journal-entry trail for every implemented
financial event. API6 exposes the entries with cursor pagination, and the
health reconciliation check compares the AR subledger with the AR control
account. A scheduled reconciliation and alerting job is future hardening.

```
GET /journal-entries‚Ü'invoice=1001

Invoice #1001 ‚Ä" Complete Financial History:

Date       | Entry | Type             | Debit               | Credit
-----------|-------|------------------|---------------------|--------------------
Jan 15     | JE001 | Invoice Approved | 1200 AR ‚Çπ1,74,000  | 3100 Rev ‚Çπ1,50,000 + 2200 Tax Payable ‚Çπ24,000
Jan 20     | JE002 | Payment Received | 1100 Cash ‚Çπ1,00,000 | 1200 AR ‚Çπ1,00,000
Jan 25     | JE003 | Credit Memo      | 3100 Rev ‚Çπ10,000   | 1200 AR ‚Çπ10,000
-----------|-------|------------------|---------------------|--------------------
Net AR outstanding: ‚Çπ64,000
```

**Reconciliation Check:**
```
Reconciliation Job ‚Ä" Jan 31, 2024 02:00am

GL Account 1200 (AR):
Sum of all journal entry lines = ‚Çπ2,34,000

AR Subledger:
Invoice #1001  Tata Steel   ‚Çπ64,000    (partially paid)
Invoice #1002  JSW Steel    ‚Çπ1,40,000  (open)
Invoice #1003  Adani Ports  ‚Çπ30,000    (open)
                            ----------
Total:                      ‚Çπ2,34,000

GL = Subledger‚Ü' ‚úÖ Books are clean!

Mismatch scenario:
GL shows:        ‚Çπ2,34,000
Subledger shows: ‚Çπ2,44,000
Difference:      ‚Çπ10,000 ‚ùå ALERT! Finance team notified immediately
```

**Database Transaction Guarantee:**
```
BEGIN TRANSACTION
  INSERT INTO invoices...
  INSERT INTO journal_entries...
COMMIT
‚Üê both succeed or both fail. never partial!
```

**Journal Entry Rules:**
- Application code treats posted entries as append-only
- Always balanced ‚Ä" debits = credits
- Always referenced ‚Ä" every JE links to source document
- Long-term immutable retention is production hardening

### Future Enhancements (Phase 2)
- Scheduled reconciliation, alerting and operational ownership
- Database privilege hardening to prohibit journal update/delete
- Automated mismatch correction
- Kafka ‚Ü' S3 WORM archival for 7 year retention
- Reconciliation dashboard

---

## FR10 ‚Ä" Multi-tenant Isolation

System must completely isolate data between tenants. No tenant can ever see another tenant's data. Isolation enforced at both application layer (JWT) and database layer (Row Level Security). Every table contains tenant_id. Every query filters by tenant_id.

**Tenant Hierarchy:**
```
Tenant (Reliance)                ‚Üê one contract, one JWT
‚"ú‚"Ä‚"Ä Entity (Reliance Retail)     ‚Üê entity_id in JWT
‚"ú‚"Ä‚"Ä Entity (Reliance Jio)
‚""‚"Ä‚"Ä Entity (Reliance Industries)
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
‚Ü' SELECT * FROM invoices
  WHERE tenant_id = 'reliance'        ‚Üê from JWT
  AND entity_id = 'reliance-retail'   ‚Üê from JWT
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
‚""‚"Ä‚"Ä Site                ‚""‚"Ä‚"Ä Entity
    ‚""‚"Ä‚"Ä Device              ‚""‚"Ä‚"Ä Invoice/Payment

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

## FR11 ‚Ä" Multi-currency

**Implementation status:** `COMPLETE` for V1 ‚Ä" ingestion, invoice/payment rate
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
Worker derives and stores tenant-approved foreign‚Ü'INR rates:

from_currency:  USD
to_currency:    INR
rate:           83.00
effective_date: 2024-01-01
source:         ECB_DAILY_REFERENCE
```

**Invoice Amounts (both stored):**
```
Invoice #2001
Reliance USA ‚Ü' US Customer

transaction_currency: USD
transaction_amount:   $1,000    ‚Üê sent to customer
exchange_rate:        83.00     ‚Üê locked on invoice date
base_currency:        INR
base_amount:          ‚Çπ83,000   ‚Üê for CFO reporting
```

**Payment with FX Gain/Loss:**
```
Invoice date:  Jan 1   rate=83  $1,000 = ‚Çπ83,000
Payment date:  Mar 1   rate=86  $1,000 = ‚Çπ86,000
FX Gain = ‚Çπ3,000

GL Entry on payment:
Debit:  1100 Cash          ‚Çπ86,000  ‚Üê actual cash received
Credit: 1200 AR            ‚Çπ83,000  ‚Üê original invoice amount
Credit: 4300 FX Gain/Loss  ‚Çπ3,000   ‚Üê difference
```

**CFO Consolidated Report:**
```
Reliance Group AR ‚Ä" Jan 31, 2024

Entity                | Currency | Amount      | INR Equivalent
----------------------|----------|-------------|----------------
Reliance Retail (IN)  | INR      | ‚Çπ1,74,000   | ‚Çπ1,74,000
Reliance USA          | USD      | $1,000      | ‚Çπ83,000
Reliance Sri Lanka    | LKR      | LKR 50,000  | ‚Çπ10,250
----------------------|----------|-------------|----------------
Total (INR)           |          |             | ‚Çπ2,67,250

FX Summary:
FX Loss:  -‚Çπ1,800
FX Gain:  +‚Çπ4,800
Net:      +‚Çπ3,000
```

Missing Exchange Rate Handling:
If rate not found for invoice date:
‚Ü' Use latest APPROVED prior-business-day rate only when <= 3 days old
‚Ü' Disclose exact rate ID, provider date and prior-date warning
‚Ü' If missing/older than 3 days, reject with FX_RATE_UNAVAILABLE
‚Ü' Never substitute 1.0 for a foreign-currency pair

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

## FR12 ‚Ä" Audit Trail

Financial record changes are captured in PostgreSQL `audit_log` by database
triggers, including actor, action, timestamp, and old/new values. This is the
implemented V1 queryable audit trail. Restrictive database privileges and
seven-year WORM archival remain production hardening.

```
GET /audit-log‚Ü'record_type=invoice&record_id=1001

Invoice #1001 ‚Ä" Audit Trail:

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
Audit table (PostgreSQL) ‚Ü' implemented; quick audit queries
Kafka/event stream       ‚Ü' future; replay and archival pipeline
S3 Object Lock/WORM      ‚Ü' future; seven-year immutable retention
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

## FR13 ‚Ä" Accounting Period Enforcement

The implemented scope prevents posting to non-OPEN monthly/yearly periods at
both application and database-trigger levels. Two levels of closure are
modelled: CLOSED (eligible for a future audited CFO reopen workflow) and LOCKED
(permanent after audit/tax filing).

```
Accounting Periods ‚Ä" Reliance Retail:

Period    | Start      | End        | Status | Closed By | Closed At
----------|------------|------------|--------|-----------|------------------
Jan 2024  | 2024-01-01 | 2024-01-31 | LOCKED | CFO Priya | 2024-03-01
Feb 2024  | 2024-02-01 | 2024-02-28 | CLOSED | CFO Priya | 2024-03-05
Mar 2024  | 2024-03-01 | 2024-03-31 | OPEN   | -         | -

Rahul tries to approve invoice dated Jan 28 on Feb 5:
‚ùå "January 2024 is LOCKED. Cannot post.
    Use a CFO-approved current-period adjustment."
```

**Two Lock Levels:**
```
CLOSED ‚Ü' CFO can reopen (forgot one invoice etc)
LOCKED ‚Ü' permanent. tax filed. auditor signed off.
         nobody can reopen. not even CFO!
```

**Enforcement:**
```python
# App level
if period.status in ['CLOSED', 'LOCKED']:
    raise PeriodClosedException()

# DB level trigger on journal_entries
BEFORE INSERT ‚Ü' verify period is OPEN
```

**Prior Period Corrections:**
```
January LOCKED but error found‚Ü'
‚Ü' Cannot reopen January
‚Ü' Post correction in current open period (March)
‚Ü' Preserve document_date = original January date
‚Ü' Set entry_date (posting date) = date in current open period
‚Ü' With clear reference to original January entry
‚Ü' Auditor sees full correction trail
```

Controls: CFO approval, mandatory reason, reference to the original invoice and
locked period, creator != approver, and an immutable audit trail. New journal
entries are never backdated into a LOCKED period.

---

## FR14 ‚Ä" RBAC and Segregation of Duties

Role-based authorization is implemented for financial APIs. JWT roles determine
allowed actions, and SOX segregation prevents the same user from creating and
approving an invoice.

**Roles:**
```
invoice_creator   ‚Ü' create/edit draft invoices
invoice_approver  ‚Ü' approve/reject invoices
payment_recorder  ‚Ü' record payments
cfo               ‚Ü' write-offs and financial administration
auditor           ‚Ü' read-only access to financial APIs
```

**Example:**
```
Rahul ‚Ü' role: invoice_creator
Priya ‚Ü' role: invoice_approver, cfo

POST /invoices/{id}/approve
Request by: Rahul (invoice_creator)
Response: 403 Forbidden

Request by: Priya (invoice_approver)
Response: 200 OK

SOX segregation:
created_by != approved_by ‚Ü' enforced in code and DB
```

---

## Future-FR1 ‚Ä" Intercompany Consolidation

**Why deferred:** The assessment's core AR flow ends at the seller entity;
buyer-side AP posting, matching and consolidation require a separate
cross-entity accounting workflow. **Plan:** Yes ‚Ä" targeted for V2 using an
audited consolidation ledger without rewriting statutory entity books.

Multi-entity scoping is implemented under FR10. Intercompany buyer-side
posting, matching, elimination and consolidated reporting are a future
capability. Each entity must retain its own statutory books.

```
GET /reports/consolidated‚Ü'tenant_id=T001&as_of=2024-01-31
GET /reports/entity‚Ü'entity_id=E002&as_of=2024-01-31

Reliance Retail (E002) invoices Reliance Jio (E003)
‚Çπ10,00,000 for IT services

Invoice #3001:
tenant_id:          T001
sender_entity_id:   E002  ‚Üê Reliance Retail
receiver_entity_id: E003  ‚Üê Reliance Jio
amount:             ‚Çπ10,00,000
is_intercompany:    TRUE  ‚Üê auto detected!

Reliance Retail books (E002):
Debit:  AR         ‚Çπ10,00,000
Credit: Revenue    ‚Çπ10,00,000

Reliance Jio books (E003):
Debit:  IT Expense ‚Çπ10,00,000
Credit: AP         ‚Çπ10,00,000

Consolidated Report (T001):
                 Retail        Jio          Elimination   Consolidated
Revenue:         ‚Çπ10,00,000   ‚Çπ0           -‚Çπ10,00,000   ‚Çπ0
Expense:         ‚Çπ0           ‚Çπ10,00,000   -‚Çπ10,00,000   ‚Çπ0
AR:              ‚Çπ10,00,000   ‚Çπ0           -‚Çπ10,00,000   ‚Çπ0
AP:              ‚Çπ0           ‚Çπ10,00,000   -‚Çπ10,00,000   ‚Çπ0
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
‚Ü' is_intercompany = TRUE
‚Ü' eliminate in consolidated report
```

**External Customer (not intercompany):**
```
Reliance Retail ‚Ü' Tata Steel
tenant_id:         T001
sender_entity_id:  E002
customer_id:       tata-steel  ‚Üê external customer!
is_intercompany:   FALSE ‚úÖ
```

### Future Enhancements (Phase 2)
- Intercompany reconciliation and matching (Retail AR = Jio AP‚Ü')
- Audited, idempotent elimination batches in a separate consolidation ledger
- Mismatch workflow instead of silently eliminating unmatched balances
- Transfer pricing compliance
- Minority interest handling
- Intercompany netting (offset AR vs AP between entities)
- Consolidated report export

---

## Future-FR2 ‚Ä" Manual Journal Entry

**Why deferred:** Manual posting can bypass source-document controls and needs
a dedicated maker-checker workflow, approval limits and reversal handling;
these were outside the prototype's required APIs. **Plan:** Yes ‚Ä" targeted for
V2 after those controls and period-management APIs exist.

System must support manual journal entries for corrections, prior period adjustments, and write-off corrections. Manual entries require CFO approval and mandatory description. Full audit trail captured. Cannot be posted to locked periods.

```
POST /journal-entries/manual
GET  /journal-entries/manual‚Ü'status=pending
POST /journal-entries/manual/{id}/approve

Scenario: Reconciliation found ‚Çπ10,000 mismatch
          GL shows ‚Çπ2,44,000, Subledger shows ‚Çπ2,34,000

Manual Journal Entry #MJE001:
Created by:  Rahul (Accounts Officer)
Approved by: CFO Priya
Period:      February 2024 (OPEN)
Description: "Correction for duplicate AR entry
              found during Jan reconciliation"

Entry:
Debit:  1200 AR        ‚Çπ10,000
Credit: 3100 Revenue   ‚Çπ10,000

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

## Future-FR3 ‚Ä" Period Management APIs

**Why deferred:** Posting into non-OPEN periods is already prevented, while
closing, locking and reopening periods are administrative workflows requiring
CFO authorization, mandatory reasons and stronger audit tests. **Plan:** Yes ‚Ä"
targeted for V2; enforcement remains active in V1.

The posting enforcement exists; administrative workflow endpoints are
deliberately deferred:

```text
POST /periods/{id}/close    ‚Üê CFO closes period
POST /periods/{id}/lock     ‚Üê permanent lock after audit
POST /periods/{id}/reopen   ‚Üê CFO reopens CLOSED with an audited reason
GET  /periods               ‚Üê list periods and status
```

- Automated period-close checklist and multiple sign-offs
- Soft close versus hard close
- Period-close exception report
- Automatic creation/opening of the next period

---

## Future-FR4 ‚Ä" User Management APIs

**Why deferred:** V1 consumes signed JWT identities and seeded roles; secure
user provisioning, credential lifecycle and role administration belong behind
an enterprise identity provider rather than the AR service itself. **Plan:**
Yes ‚Ä" V2 will integrate an IdP and expose only the required ERP role-mapping
administration.

Role administration is not part of the implemented prototype:

```
POST /users                    ‚Üê create user
POST /users/{id}/roles         ‚Üê assign role
DELETE /users/{id}/roles/{role}‚Üê revoke role
GET  /users                    ‚Üê list users
```

### Future Enhancements (Phase 2)
- Entity level permissions (approve invoices for Retail but not Jio)
- Role hierarchies
- Permission inheritance
- Temporary role delegation (CFO on leave)
