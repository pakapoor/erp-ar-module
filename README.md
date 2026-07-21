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

First time setup (builds everything and verifies):
```bash
./deploy.sh --seed --test
```

Day-to-day (already built, just restart):
```bash
./deploy.sh --no-build
```

---

## What This Is

A production-quality AR module implementing the complete invoice-to-cash cycle:

```
Raise invoice -> Approve -> Post to GL -> Receive payment -> Age receivables -> Audit trail
```

### APIs Implemented

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /invoices | Create invoice with line items |
| GET | /invoices/{id} | Retrieve invoice with payment history |
| POST | /invoices/{id}/approve | Approve and generate GL journal entry |
| POST | /payments | Record payment and FIFO/manual allocation |
| GET | /customers/{id}/aging | AR aging report (current/30/60/90+ days) |
| GET | /journal-entries | GL journal entries for an invoice |
| GET | /health | DB health and AR-to-GL reconciliation |
| POST | /invoices/{id}/credit-memos | Credit memo with GL reversal |
| POST | /invoices/{id}/writeoff | CFO bad-debt write-off |
| POST | /invoices/{id}/void | Void draft or reverse posted invoice |

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

---

## Tech Stack

- Gateway: Envoy
- Backend: Python / FastAPI
- Database: PostgreSQL 16 with pg_cron and Row Level Security
- Messaging: LocalStack SQS with dead-letter queue
- Containers: Docker Compose (8 services)
- AI tools: Claude (domain learning + design), GitHub Copilot + Codex (code)

---

## Tests

**200 checks passing. 70.9% code coverage.**

### Integration tests (127 assertions)

| Suite | Assertions | What it tests |
|-------|-----------|---------------|
| `tests/integration/test_api.sh` | 62 | All 6 required APIs, FX invoices/payments, accounting correctness, idempotency, cross-tenant isolation, AR-to-GL reconciliation |
| `tests/integration/test_api_negative.sh` | 34 | Malformed JWTs, bad headers, hostile JSON, post-burst health check |
| `tests/integration/test_credit_memo.sh` | 17 | Credit memo entity isolation, idempotency, concurrent over-credit, paid/partial invoices, FX and GL balance |
| `tests/integration/test_delivery_sqs.sh` | 11 | Outbox -> SQS -> consumer flow, DLQ redrive, downstream deduplication, CFO retry of DEAD event |
| `tests/concurrency/test_payment_concurrency.sh` | 3 | Two concurrent payments race AUTO and MANUAL allocation -- exactly one commits, loser gets 409 |

### Unit tests (73 tests)

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
| Overall | **70.9% (gate: 70%)** |

Run coverage:
```bash
./tests/run_coverage.sh
```

---

## Documentation

Start here for a full walkthrough:

- [Technical Walkthrough](docs/walkthrough.md) - narrative + Q&A
- [Requirements Traceability](docs/requirements-traceability.md) - what is done vs deferred
- [High Level Design](docs/high-level-design.md) - architecture diagrams
- [API Design](docs/api-design.md) - endpoint contracts
- [Data Model](docs/data-model.md) - ER diagrams and schema decisions
- [Design Tradeoffs](docs/tradeoffs.md) - 14 documented decisions
- [Financial Controls](docs/financial-controls.md) - SOX, period close, reconciliation
- [FX Rate Design](docs/fx-rate-design.md) - multi-currency approach
- [Experience Showcase](docs/experience-showcase.md) - Meta + Lenovo context
- [Assessment Submission](docs/assessment-submission.md) - consolidated narrative
- [Deployment Guide](docs/deployment.md) - local setup
- [Test Results](docs/tests.md) - coverage dashboard

---

## Time Investment

The assessment suggested 3-4 hours. I spent a full weekend -- financial systems were
a new domain and I wanted to genuinely understand the accounting before writing code.

- Domain learning: double-entry bookkeeping, GL/AR reconciliation, SOX, period close
- Design: FRs, NFRs, ER diagrams, API contracts, 14 tradeoffs documented
- Implementation: iterative build with real bugs fixed (isolation ordering, JWT kid, concurrent approval race)
- Financial controls + docs: written from real experience at Meta and Lenovo

AI tools made this depth achievable in the time available -- which is the point of encouraging their use.

---

## Project Structure

```
erp-ar-module/
|-- deploy.sh                    Build, migrate, start, verify
|-- docker-compose.yml           8-service local deployment
|-- gateway/envoy.yaml           L7 gateway config
|-- migrations/                  9 SQL migrations
|-- src/
|   |-- main.py                  FastAPI app + middleware
|   |-- auth.py                  JWT/JWKS + RBAC
|   |-- models/models.py         ORM models
|   |-- schemas.py               Request/response contracts
|   |-- seed_data.py             Deterministic test data
|   |-- delivery_publisher.py    Outbox to SQS
|   |-- delivery_worker.py       SQS to adapter consumer
|   |-- fx_rate_worker.py        ECB rate ingestion
|   +-- routers/
|       |-- invoices.py
|       |-- payments.py
|       |-- credit_memos.py
|       |-- aging.py
|       |-- journal_entries.py
|       |-- delivery.py
|       +-- health.py
|-- tests/
|   |-- unit/
|   |-- integration/
|   +-- concurrency/
+-- docs/
    |-- walkthrough.md
    |-- architecture.svg
    |-- high-level-design.md
    |-- flows/                   API flow diagrams
    |-- data-model.md
    |-- api-design.md
    |-- tradeoffs.md
    |-- financial-controls.md
    |-- FRs.md and NFRs.md
    |-- fx-rate-design.md
    |-- experience-showcase.md
    |-- assessment-submission.md
    |-- requirements-traceability.md
    |-- deployment.md
    +-- tests.md
```
