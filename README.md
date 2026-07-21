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

200 checks passing. 70.9% code coverage.

### Integration (127 assertions)

| Suite | Assertions | What it tests |
|-------|-----------|---------------|
| `tests/integration/test_api.sh` | 62 | All 6 required APIs, FX invoices/payments, accounting correctness, idempotency, cross-tenant isolation, AR-to-GL reconciliation |
| `tests/integration/test_api_negative.sh` | 34 | Malformed JWTs, bad headers, hostile JSON, post-burst health check |
| `tests/integration/test_credit_memo.sh` | 17 | Credit memo entity isolation, idempotency, concurrent over-credit, paid/partial invoices, FX and GL balance |
| `tests/integration/test_delivery_sqs.sh` | 11 | Outbox -> SQS -> consumer flow, DLQ redrive, downstream deduplication, CFO retry of DEAD event |
| `tests/concurrency/test_payment_concurrency.sh` | 3 | Two concurrent payments race AUTO and MANUAL -- exactly one commits, loser gets 409 |

### Unit (73 tests)

| File | What it covers |
|------|---------------|
| `test_auth.py` | JWT validation, RBAC, kid lookup, token expiry |
| `test_endpoints.py` | API response shapes and status codes |
| `test_financial_guards.py` | SOX checks, period close, credit limit, idempotency guards |
| `test_fx_rate_worker.py` | ECB rate ingestion, staleness, currency pairs |
| `test_invoice_fx.py` | FX invoice creation, base currency amounts |
| `test_router_helpers.py` | Aging builder, health reconciliation logic |
| `test_seed_and_database.py` | Seed data integrity, RLS enforcement |
| `test_worker_persistence.py` | Outbox persistence, crash recovery |
| `test_workers.py` | SQS consumer, publisher, DLQ redrive |

### Coverage (verified 20 July 2026)

| Module | Coverage |
|--------|---------|
| `src/routers/aging.py` | 100% |
| `src/routers/delivery.py` | 98% |
| `src/routers/journal_entries.py` | 96% |
| `src/seed_data.py` | 94% |
| `src/main.py` | 89% |
| `src/delivery_worker.py` | 87% |
| `src/routers/health.py` | 81% |
| `src/fx_rate_worker.py` | 79% |
| Overall | 70.9% (gate: 70%) |

See [Test Results](docs/tests.md) for dashboard summary and [Test Catalog](docs/test-catalog.md) for comprehensive details (all categories, individual tests, how to run, descriptions, and coverage).

---

## Documentation

- [Walkthrough](docs/walkthrough.md) -- full narrative, one transaction end-to-end, Q&A
- [Requirements Traceability](docs/requirements-traceability.md) -- what is implemented vs deferred
- [Experience Showcase](docs/experience-showcase.md) -- Meta and Lenovo context for design decisions
- [Deployment Guide](docs/deployment.md) -- local setup and troubleshooting

---

## Time Investment

Spent a full weekend -- financial systems were a new domain and I wanted to
genuinely understand the accounting before writing code.

- Domain learning: double-entry bookkeeping, GL/AR reconciliation, SOX, period close
- Design: FRs, NFRs, ER diagrams, API contracts, 14 tradeoffs documented
- Implementation: iterative build with real bugs fixed (isolation ordering, JWT kid, concurrent approval race)
- Docs and controls: written from real experience at Meta and Lenovo

AI tools made this depth achievable in the time available.
