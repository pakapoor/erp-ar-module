# Claude Session Context -- ERP AR Module Debug Session
Generated: 2026-07-22

## Project
~/projects/erp-ar-module
Multi-tenant AR module for DeepRunner.ai take-home assessment.
Stack: FastAPI + PostgreSQL 16 + LocalStack SQS + Envoy + Docker Compose (8 services)
GitHub: push to main, single branch

## What Was Built (Assessment)
All 6 required APIs implemented + significant extras:
- POST /invoices
- GET /invoices/{id} (with payment_history, credit_memo_history, status_history)
- POST /invoices/{id}/approve  -- now supports action: APPROVE/REJECT
- PATCH /invoices/{id}         -- NEW: edit DRAFT invoices, clears rejection_reason
- POST /payments (AUTO/FIFO + MANUAL allocation)
- GET /customers/{id}/aging
- GET /journal-entries?invoice={id}
- GET /health (AR-to-GL reconciliation)
- POST /invoices/{id}/credit-memos
- POST /invoices/{id}/writeoff
- POST /invoices/{id}/void
Delivery: transactional outbox -> SQS -> delivery_worker -> stub
FX: fx_rate_worker ingests ECB rates, staleness enforced

## Recent Changes (this session)
1. POST /invoices/{id}/approve now accepts:
   {action: 'APPROVE', notes: '...'} -- default, existing behavior
   {action: 'REJECT', rejection_reason: '...'} -- NEW
   - REJECT: invoice stays DRAFT, rejection_reason saved, NO GL entry, NO delivery
   - SOX: rejector != creator enforced same as approver
   - Schema: InvoiceApprove in schemas.py updated with action + rejection_reason fields
   - rejection_reason required when action=REJECT (validator in schema)

2. PATCH /invoices/{id} -- NEW endpoint
   - Only on DRAFT invoices (covers new + rejected)
   - Role: invoice_creator only
   - Fields: invoice_date, payment_terms, po_reference, line_items (all optional)
   - If line_items provided: old items deleted, new inserted, totals recalculated
   - FX rate re-resolved if currency/date changes
   - Credit limit re-checked
   - Clears rejection_reason on save
   - Optimistic locking via If-Match header
   - Idempotency via X-Idempotency-Key
   - Schema: InvoiceUpdate in schemas.py
   - Payments NOT allowed on DRAFT (enforced separately)

3. Debug session system (all under debug/):
   - setup_debug_session.py: creates fresh isolated tenant+entity+3users+7GL+period+customer
     Copied into container, session file copied back to host
     Saves to debug/.debug_session (gitignored)
   - debug/.debug_session: JSON with tenant_id, entity_id, user_creator_id,
     user_approver_id, user_payer_id, customer_id, invoice1_id, invoice2_id, last_invoice_id

4. debug/invoice/ scripts (all use .debug_session, all have --max-time 600):
   1_create_invoice.sh   -- runs setup, creates 2 invoices (NET15 + NET30 for FIFO test)
   2_get_invoice.sh      -- no arg: both invoices; with arg: specific UUID
   3_approve_invoice.sh  -- no arg: approves both; with arg: specific UUID
   3_2_reject_invoice.sh -- rejects invoice (created by VSCode agent)
   3_1_patch_invoice.sh  -- patches DRAFT invoice (created by VSCode agent)
   4_pay_invoice.sh      -- requires amount arg, AUTO/FIFO, NEFT method
   5_journal_entries.sh  -- no arg: both invoices; with arg: specific UUID

5. Documentation updated:
   docs/api-design.md: added API2b (PATCH) and API3b (reject flow) sections at lines 562/656

6. .gitignore: debug/.debug_session and debug/.last_invoice_id excluded

## Key Architecture Decisions
- No Redis: PostgreSQL owns all state, locks, idempotency
- Modular monolith: invoice+payment+GL in one ACID transaction
- Transactional outbox: delivery committed with invoice, not SQS-dependent
- Zero Trust: JWT validated at Envoy AND FastAPI independently
- Serializable isolation for payments (concurrent payment protection)
- Optimistic locking (version + If-Match) on all write operations
- SOX segregation: creator != approver/rejector, payment_recorder separate role

## Debug Flow (working end-to-end verified)
./debug/invoice/1_create_invoice.sh       # fresh session + 2 invoices
./debug/invoice/3_approve_invoice.sh      # approves both, triggers delivery
docker compose logs stub -f               # see 2 x delivery banners
./debug/invoice/4_pay_invoice.sh 118000   # FIFO pays invoice1 (NET15) first
./debug/invoice/2_get_invoice.sh          # payment_history populated
./debug/invoice/5_journal_entries.sh      # GL entries: Dr AR/Cr Revenue/Cr Tax + Dr Cash/Cr AR

## Reject -> Edit -> Reapprove Flow
./debug/invoice/1_create_invoice.sh
./debug/invoice/3_2_reject_invoice.sh <invoice_id>   # back to DRAFT
./debug/invoice/3_1_patch_invoice.sh <invoice_id>    # fix it
./debug/invoice/3_approve_invoice.sh <invoice_id>    # reapprove

## DB Connection
docker compose exec db psql -U erp_user -d erp_db -c '<query>'
Tables: tenant, entity, gl_account, customer, app_user, accounting_period,
        invoice, invoice_line_item, payment, payment_allocation,
        journal_entry, journal_entry_line, credit_memo, audit_log,
        idempotency_key, delivery_outbox, fx_import_job, exchange_rate

## Token Generation Pattern
docker compose exec -T app python -c "from src.auth import create_test_token; print(create_test_token('<user_id>','<tenant_id>','<entity_id>',['<role>']))"
Roles: invoice_creator, invoice_approver, cfo, payment_recorder

## VSCode Debug
Launch config: 'Debug FastAPI in Docker' (debugpy, port 5678)
All debug scripts have --max-time 600 for breakpoint stepping

## Tests
225 checks passing. 72.6% coverage (gate: 70%)
Unit: tests/unit/ (84 tests, includes test_invoice_patch_reject.py)
Integration: tests/integration/ (138 assertions across 4 suites, includes API3b reject/patch/re-approve in test_api.sh)
Concurrency: tests/concurrency/
Run: ./deploy.sh --test OR ./tests/run_coverage.sh

## Delivery Stub
Service: stub (docker compose logs stub -f)
Endpoint: POST /stub/send-invoice
Fires on: invoice APPROVAL only (not payment)
Logs: emoji banner with invoice_id, amount, customer, due_date

## Pending / Known
- FRs.md mentions reject but was not in original implementation -- now done
- 3_1_patch_invoice.sh and 3_2_reject_invoice.sh verified by Claude: fixed a
  hardcoded If-Match: 1 bug in 3_approve_invoice.sh/3_2_reject_invoice.sh that
  broke reject->patch->reapprove on any invoice past version 1
- rejection_reason clearing in PATCH confirmed in code review
- PATCH/reject now covered by tests/unit/test_invoice_patch_reject.py and the
  API3b section of tests/integration/test_api.sh
- All changes committed to main and pushed
