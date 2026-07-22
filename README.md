# ERP AR Module

Multi-tenant Invoicing and Accounts Receivable module for mid-market ERP systems.

---

## Quick Start

| Command | What it does |
|---------|-------------|
| `./deploy.sh` | Build images and start all 8 services |
| `./deploy.sh --no-build` | Start services reusing existing images (faster) |
| `./deploy.sh --seed` | Start services and load demo data |
| `./deploy.sh --test` | Start services and run full integration suite |
| `./deploy.sh --seed --test` | Start, seed demo data, then run tests |
| `./deploy.sh --no-build --test` | Skip rebuild, run tests against running stack |
| `./tests/run_coverage.sh` | Run unit tests with 70% coverage gate |

First time setup:
```bash
./deploy.sh --seed --test
```

Day-to-day (already built):
```bash
./deploy.sh --no-build
```

---

## What This Is

A production-quality AR module implementing the complete invoice-to-cash cycle:

```
Raise invoice -> Approve -> Post to GL -> Receive payment -> Age receivables -> Audit trail
```

### APIs

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /invoices | Create invoice with line items |
| GET | /invoices/{id} | Retrieve invoice with payment history |
| PATCH | /invoices/{id} | Edit a DRAFT invoice (creator only) |
| POST | /invoices/{id}/approve | Approve (generates GL journal entry) or reject (`action: REJECT`, reverts to DRAFT) |
| POST | /payments | Record payment and FIFO/manual allocation |
| GET | /customers/{id}/aging | AR aging report (current/30/60/90+ days) |
| GET | /journal-entries | GL journal entries for an invoice |
| GET | /health | DB health and AR-to-GL reconciliation |
| POST | /invoices/{id}/credit-memos | Credit memo with GL reversal |
| POST | /invoices/{id}/writeoff | CFO bad-debt write-off |
| POST | /invoices/{id}/void | Void draft or reverse posted invoice |

See [API Design](docs/api-design.md) for full contracts and error codes.

---

## Functional Requirements

| ID | Requirement |
|---|---|
| FR1 | Create invoice with multi-line items, tax calculation, currency support |
| FR2 | Approve invoice — generates GL journal entry atomically; Reject reverts to DRAFT |
| FR2b | Edit DRAFT/REJECTED invoice via PATCH — replaces line items, recalculates totals |
| FR3 | Record payment with FIFO auto-allocation, partial payment, overpayment handling |
| FR4 | Deliver invoice to customer via transactional outbox → SQS (non-blocking) |
| FR5 | Issue credit memo with GL reversal — reduces AR without cancelling invoice |
| FR-B1 | Write off uncollectible invoice — posts bad debt expense to GL |
| FR-B2 | Void invoice — full GL reversal; allowed on DRAFT/APPROVED/SENT only |
| FR-B3 | AR Aging report — buckets by due date (current, 1-30, 31-60, 61-90, 90+) |

---

## Non-Functional Requirements

| NFR | Approach |
|---|---|
| Multi-tenancy | Every table has tenant_id; RLS enforces isolation at DB level |
| Idempotency | X-Idempotency-Key required on all write APIs; key + hash stored atomically |
| Optimistic concurrency | If-Match versioning on approve/patch — prevents lost updates |
| SOX segregation of duties | created_by != approved_by enforced in application layer |
| Payment correctness | SERIALIZABLE isolation prevents double-allocation across concurrent payments |
| Guaranteed delivery | Transactional outbox commits delivery event with invoice in same ACID transaction |
| Audit trail | DB-level audit triggers fire on all state changes — cannot be bypassed by app code |
| Test coverage | 70%+ coverage gate enforced in CI |

---

## Architecture

![System Architecture](docs/architecture.svg)

```
Client
  -> Envoy L7 Gateway       (JWT validation, rate limiting, trace IDs)
  -> FastAPI AR Application  (Zero Trust JWT re-validation, business logic)
  -> PostgreSQL              (single source of financial truth)
       |
  delivery_outbox -> SQS -> consumer -> adapter
```

Key decisions:

- No Redis: PostgreSQL owns all financial state, locks, and idempotency
- Modular monolith: invoice + payment + GL share one ACID transaction
- Transactional outbox: delivery committed with invoice, not dependent on SQS
- Zero Trust: JWT validated at both gateway and application layer independently

Go deeper:

- [High Level Design](docs/high-level-design.md) -- component diagram, all 7 API flows
- [Data Model](docs/data-model.md) -- ER diagrams, schema decisions, RLS design
- [Design Tradeoffs](docs/tradeoffs.md) -- 14 documented decisions with reasoning
- [Financial Controls](docs/financial-controls.md) -- SOX, period close, GL reconciliation
- [FX Rate Design](docs/fx-rate-design.md) -- multi-currency approach

Stack: Envoy, Python/FastAPI, PostgreSQL 16 (pg_cron + RLS), LocalStack SQS, Docker Compose (8 services)

---

## Tests

267 checks passing. 82.3% code coverage.

### Integration (141 assertions)

| Suite | Assertions | What it tests |
|-------|-----------|---------------|
| `tests/integration/test_api.sh` | 76 | All 6 required APIs, PATCH/reject/re-approve, FX invoices/payments, accounting correctness, idempotency, cross-tenant isolation, AR-to-GL reconciliation |
| `tests/integration/test_api_negative.sh` | 34 | Malformed JWTs, bad headers, hostile JSON, post-burst health check |
| `tests/integration/test_credit_memo.sh` | 17 | Credit memo entity isolation, idempotency, concurrent over-credit, paid/partial invoices, FX and GL balance |
| `tests/integration/test_delivery_sqs.sh` | 11 | Outbox -> SQS -> consumer flow, DLQ redrive, downstream deduplication, CFO retry of DEAD event |
| `tests/concurrency/test_payment_concurrency.sh` | 3 | Two concurrent payments race AUTO and MANUAL -- exactly one commits, loser gets 409 |

### Unit (126 tests)

| File | What it covers |
|------|---------------|
| `test_auth.py` | JWT validation, RBAC, kid lookup, token expiry, JWKS fetch failure |
| `test_credit_memo_happy_path.py` | Full and over-balance credit memo GL reversal, customer credit liability |
| `test_endpoints.py` | API response shapes, status codes, health check happy/degraded/unhealthy paths |
| `test_financial_guards.py` | SOX checks, period close, credit limit, idempotency guards |
| `test_fx_rate_worker.py` | ECB rate ingestion, staleness, currency pairs, CSV validation edge cases |
| `test_get_invoice.py` | GET /invoices/{id} full response, history sections, ETag |
| `test_invoice_create.py` | POST /invoices guard clauses and server-calculated happy path |
| `test_invoice_fx.py` | FX invoice creation, base currency amounts |
| `test_invoice_patch_reject.py` | PATCH /invoices/{id} and reject (action: REJECT) guard clauses and happy paths |
| `test_payment_helpers.py` | FX rate resolution, FIFO/manual allocation, retryable conflict detection |
| `test_router_helpers.py` | Aging builder, health reconciliation logic, schema validation |
| `test_seed_and_database.py` | Seed data integrity, RLS enforcement |
| `test_worker_persistence.py` | Outbox persistence, crash recovery |
| `test_workers.py` | SQS consumer, publisher, DLQ redrive |

### Coverage (verified 22 July 2026)

| Module | Coverage |
|--------|---------|
| `src/routers/aging.py` | 100% |
| `src/routers/health.py` | 98% |
| `src/routers/delivery.py` | 98% |
| `src/schemas.py` | 98% |
| `src/routers/journal_entries.py` | 96% |
| `src/seed_data.py` | 94% |
| `src/auth.py` | 91% |
| `src/main.py` | 89% |
| `src/delivery_worker.py` | 87% |
| `src/fx_rate_worker.py` | 84% |
| `src/routers/invoices.py` | 80% |
| `src/routers/credit_memos.py` | 62% |
| `src/routers/payments.py` | 43% |
| Overall | 82.3% (gate: 70%) |

See [Test Results](docs/tests.md) for dashboard summary and [Test Catalog](docs/test-catalog.md) for comprehensive details (all categories, individual tests, how to run, descriptions, and coverage).

---

## Documentation

- [Walkthrough](docs/walkthrough.md) -- full narrative, one transaction end-to-end, Q&A
- [Requirements Traceability](docs/requirements-traceability.md) -- what is implemented vs deferred
- [Experience Showcase](docs/experience-showcase.md) -- Meta and Lenovo context for design decisions
- [Deployment Guide](docs/deployment.md) -- local setup and troubleshooting
- [Manual Debugging](docs/manual-debugging.md) -- curl-driven scripts in `debug/` for exercising invoice, payment, GL, and aging flows by hand

---

## Time Investment

Spent a full weekend -- financial systems were a new domain and I wanted to
genuinely understand the accounting before writing code.

- Domain learning: double-entry bookkeeping, GL/AR reconciliation, SOX, period close
- Design: FRs, NFRs, ER diagrams, API contracts, 14 tradeoffs documented
- Implementation: iterative build with real bugs fixed (isolation ordering, JWT kid, concurrent approval race)
- Docs and controls: written from real experience at Meta and Lenovo

AI tools made this depth achievable in the time available.
