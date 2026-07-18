# Functional Requirements — ERP AR Module

## Table of Contents
- [FR1 — Invoice Creation](#fr1--invoice-creation)
- [FR2 — Invoice Approval](#fr2--invoice-approval)
- [FR3 — Invoice Sending](#fr3--invoice-sending)
- [FR4 — Payment Recording and Allocation](#fr4--payment-recording-and-allocation)

## FR1 — Invoice Creation
The system must allow creation of an invoice with sender entity, receiver (customer) details, line items (description, quantity, unit price, tax rate, tax jurisdiction, tax amount, and line total), subtotal, total tax, and grand total. An optional PO reference must be supported, and each invoice must carry a currency at the invoice level. Each line item must support its own tax rate and tax jurisdiction.

Example:

```text
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

### Below the line
- E-invoicing compliance: India IRP/IRN mandate, EU Peppol network
- Full tax engine (CGST/SGST/IGST rules)
- Proforma invoice and advance payment (Phase 2)

## FR2 — Invoice Approval
An invoice must be approved by an authorized person before it can be sent to the customer. Approval authority depends on invoice amount thresholds. The system must enforce that the creator and approver are different people to support SOX compliance. On approval, GL journal entries must be generated automatically. On rejection, the invoice must return to Draft with a mandatory rejection reason.

Example:

```text
Invoice #1001 (from FR1)
Sender:   Reliance Retail, Mumbai (GSTIN: 27AAACR...)
Receiver: Tata Steel, Bangalore (GSTIN: 29AAACT...)

Step 1: Rahul (Accounts Officer) creates invoice — Status: Draft
Step 2: Rahul submits for approval
        Amount ₹1,74,000 < ₹10,00,000 → Auto-routed to Priya (Finance Manager)
        Status: Draft (is_submitted: true)
Step 3: Priya rejects
        Reason: "Line 2 Safety Valves — interstate supply must use IGST not CGST+SGST"
        Status: Draft (is_submitted: false)
Step 4: Rahul fixes Line 2 tax jurisdiction and resubmits
Step 5: Priya approves
        created_by: Rahul (user_id: 123)
        approved_by: Priya (user_id: 456)
        created_by ≠ approved_by → SOX compliant ✅

Auto GL Entry Generated:
Debit:  1200 AR        ₹1,74,000  ← Tata Steel owes Reliance
Credit: 3100 Revenue   ₹1,74,000  ← Reliance earned it
Status: Draft → Approved
```

### Below the line
- Approval SLA and escalation chain (Phase 2, needs Temporal)

## FR3 — Invoice Sending
After approval, invoice status must transition to Sent when the invoice is delivered to the customer. The system must record the sent timestamp and delivery confirmation.

Example:

```text
Invoice #1001 — Approved by Priya
Sent to: accounts@tatasteel.com
Sent at: 2024-01-15 11:00
Format:  PDF
Status:  Approved → Sent
```

### Below the line
- E-invoicing compliance: India IRP/IRN mandate, EU Peppol network, Italy mandate
- Multiple delivery methods: Email, Customer Portal, EDI
- Delivery failure retry and alerting
- B2G compliance for US government contracts

## FR4 — Payment Recording and Allocation
The system must record payments from customers and allocate them against one or more outstanding invoices. Payment allocation may be automatic (FIFO, oldest invoice first) or manual (customer specifies allocation). The system must handle partial payments, full payments, and overpayments. All payments must generate GL journal entries automatically.

Payment terms on the invoice must be captured as:
- payment_terms ← NET30/NET60/NET90
- due_date ← invoice_date + payment_terms

Example 1 — Full Payment (Auto FIFO):

```text
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
```

Example 2 — Partial Payment (Auto FIFO):

```text
Payment #P002 received: ₹2,00,000
Allocation:
├── Invoice #1001 ₹1,74,000 → Fully Paid ✅
└── Invoice #1002 ₹26,000   → Partially Paid 🔄 (₹54,000 remaining)
Invoice #1003 → Untouched ❌

GL Entry:
Debit:  1100 Cash    ₹2,00,000  (ref: P002)
Credit: 1200 AR      ₹2,00,000  (ref: P002)
```

Example 3 — Manual Allocation:

```text
Payment #P003 received: ₹2,00,000
Mode: Manual (remittance advice from Tata Steel)
Allocation:
├── Invoice #1001 ₹1,00,000
├── Invoice #1002 ₹60,000
└── Invoice #1003 ₹40,000
```

Example 4 — Overpayment:

```text
Payment #P004 received: ₹4,00,000
Total outstanding:      ₹3,00,000
Overpayment:            ₹1,00,000

GL Entry:
Debit:  1100 Cash            ₹4,00,000
Credit: 1200 AR              ₹3,00,000  ← clears all invoices
Credit: 2100 Customer Credit ₹1,00,000  ← liability, we owe them
```

### Below the line
- Installment payment schedules (Phase 2)
- Automated payment gateway webhook reconciliation (Phase 2)
- Early payment discount 2/10 NET30 (Phase 2)
