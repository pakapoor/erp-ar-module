# ERP AR Module

Multi-tenant Invoicing and Accounts Receivable module for mid-market ERP systems.

Built as a Principal Engineer technical assessment for DeepRunner.ai.

---

## Quick Start

```bash
./deploy.sh --test
```

This starts eight services and runs the full test suite.

---

## What This Is

A production-quality AR module implementing the complete invoice-to-cash cycle:

```
Raise invoice â†' Approve â†' Post to GL â†' Receive payment â†' Age receivables â†' Audit trail
```

### APIs Implemented

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /invoices | Create invoice with line items |
| GET | /invoices/{id} | Retrieve invoice with payment history |
| POST | /invoices/{id}/approve | Approve + generate GL journal entry |
| POST | /payments | Record payment + FIFO/manual allocation |
| GET | /customers/{id}/aging | AR aging report (current/30/60/90+ days) |
| GET | /journal-entries | GL journal entries for an invoice |
| GET | /health | DB health + AR-to-GL reconciliation |
| POST | /invoices/{id}/credit-memos | Credit memo with GL reversal |
| POST | /invoices/{id}/writeoff | CFO bad-debt write-off |
| POST | /invoices/{id}/void | Void draft or reverse posted invoice |

---

## Architecture

![System Architecture](docs/architecture.svg)

```
Client
  â†' Envoy L7 Gateway      (JWT validation, rate limiting, trace IDs)
  â†' FastAPI AR Application (Zero Trust JWT re-validation, business logic)
  â†' PostgreSQL             (single source of financial truth)
       â†'
  delivery_outbox â†' SQS â†' consumer â†' adapter
```

**Key decisions:**
- No Redis Ã' PostgreSQL owns all financial state, locks, and idempotency
- Modular monolith Ã' invoice + payment + GL share one ACID transaction
- Transactional outbox Ã' delivery committed with invoice, not dependent on SQS
- Zero Trust Ã' JWT validated at both gateway and application layer independently

---

## Tech Stack

- **Gateway:** Envoy
- **Backend:** Python / FastAPI
- **Database:** PostgreSQL 16 with pg_cron and Row Level Security
- **Messaging:** LocalStack SQS with dead-letter queue
- **Containers:** Docker Compose (8 services)
- **AI tools:** Claude (domain learning + design), GitHub Copilot + Codex (code)

---

## Testing

```
135 tests passing
70.9% code coverage

Integration:  test_api.sh            (required APIs + accounting assertions)
              test_credit_memo.sh    (FR-B1 credit memo controls)
              test_delivery_sqs.sh   (outbox â†' SQS â†' DLQ flow)
              test_api_negative.sh   (JWT attacks, hostile inputs)
Concurrency:  test_payment_concurrency.sh  (double-allocation race)
Unit:         tests/unit/            (73 unit tests, pytest)
```

---

## Documentation

Start here for a full walkthrough:

- [Technical Walkthrough](docs/interview-walkthrough.md) Ã' narrative + Q&A
- [Requirements Traceability](docs/requirements-traceability.md) Ã' what is done vs deferred
- [High Level Design](docs/high-level-design.md) Ã' architecture diagrams
- [API Design](docs/api-design.md) Ã' endpoint contracts
- [Data Model](docs/data-model.md) Ã' ER diagrams and schema decisions
- [Design Tradeoffs](docs/tradeoffs.md) Ã' 14 documented decisions
- [Financial Controls](docs/financial-controls.md) Ã' SOX, period close, reconciliation
- [FX Rate Design](docs/fx-rate-design.md) Ã' multi-currency approach
- [Experience Showcase](docs/experience-showcase.md) Ã' Meta + Lenovo context
- [Assessment Submission](docs/assessment-submission.md) Ã' consolidated narrative
- [Deployment Guide](docs/deployment.md) Ã' local setup
- [Test Results](docs/tests.md) Ã' coverage dashboard

---

## Time Investment

The assessment suggested 3-4 hours. I spent a full weekend Ã' financial systems were a new domain and I wanted to genuinely understand the accounting before writing code.

- **Domain learning:** double-entry bookkeeping, GL/AR reconciliation, SOX, period close
- **Design:** FRs, NFRs, ER diagrams, API contracts, 14 tradeoffs documented
- **Implementation:** iterative build with real bugs fixed (isolation ordering, JWT kid, concurrent approval race)
- **Financial controls + docs:** written from real experience at Meta and Lenovo

AI tools made this depth achievable in the time available Ã' which is the point of encouraging their use.

---

## Project Structure

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
        |-- interview-walkthrough.md
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