# ERP AR Module
A multi-tenant Invoicing and Accounts Receivable module for mid-market ERP systems.

Built as part of a Principal Engineer technical assessment for DeepRunner.ai.

## Quick Start
```bash
./deploy.sh --test
```

## Tech Stack
- **L7 API Gateway:** Envoy
- **Backend:** Python / FastAPI
- **Database:** PostgreSQL (with Row Level Security)
- **Containerization:** Docker / Docker Compose
- **AI Tools Used:** Claude (domain learning and design), GitHub Copilot (code assistance), Codex (implementation review, debugging, and integration verification)

## Design Walkthrough
For interview/review, follow this order:
- [Functional Requirements](docs/FRs.md)
- [Non Functional Requirements](docs/NFRs.md)
- [Assessment Requirements Traceability](docs/requirements-traceability.md)
- [FX Rate and Multi-Currency Design](docs/fx-rate-design.md)
- [High Level Design](docs/high-level-design.md)
- [Data Model](docs/data-model.md)
- [API Design](docs/api-design.md)
- [Design Tradeoffs](docs/tradeoffs.md)
- [Financial Controls](docs/financial-controls.md)
- [Local Deployment Guide](docs/deployment.md)
- [Verification and Expected Results](docs/testing.md)
- [Experience Showcase](docs/experience-showcase.md)
- [Consolidated Assessment Submission](docs/assessment-submission.md)

## Key Capabilities
- Envoy L7 edge gateway with early JWT rejection, request tracing, path normalization, and local rate limiting
- Zero Trust JWT validation at both Envoy and FastAPI; the application has no host-published port
- Multi-tenant data isolation (JWT + PostgreSQL RLS)
- Multi-entity-aware data model
- Transaction/base-currency and exchange-rate data model
- Balanced GL journal entries for approval and payment
- Draft, approval, partial-payment, and paid lifecycle transitions
- AR aging report
- Idempotent write APIs with cached retry responses
- Database-triggered audit trail with actor context
- Application and database period-posting controls
- Transactional invoice-delivery outbox with retrying Docker worker
- pg_cron-scheduled ECB rate ingestion with durable jobs, provenance and seven
  approved foreign→INR pairs

Designed but deferred from the required prototype: production email/EDI/IRP
delivery adapters, credit memos, write-offs, void/reissue, intercompany
elimination, manual journals, and period-management APIs. Full FX processing is
currently `IN PROGRESS`; its approved scope and acceptance tests are documented
in [FX Rate and Multi-Currency Design](docs/fx-rate-design.md).

## API Endpoints
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /invoices | Create invoice |
| GET | /invoices/{id} | Get invoice with balance |
| POST | /invoices/{id}/approve | Approve invoice + generate GL entries |
| POST | /payments | Record payment + allocate to invoices |
| GET | /customers/{id}/aging | AR aging report |
| GET | /journal-entries | GL entries for invoice |
| GET | /health | Operational health and AR/GL reconciliation |

## Verification

`test_api.sh` seeds deterministic data, creates fresh development JWTs, tests
the six required endpoints plus health, and asserts invoice totals, payment
idempotency, aging, balanced journal entries, AR-to-GL reconciliation,
transactional-outbox delivery, cross-tenant denial, RBAC denial, and
idempotency-payload conflict handling. It also verifies gateway JWT rejection,
trace propagation, application network isolation, and independent FastAPI JWT
validation.

Detailed commands, database inspection queries, expected output, pg_cron
verification, and an optional delivery-retry drill are in
[Verification and Expected Results](docs/testing.md).

## Time Tracking

This project was expanded beyond the assessment's 3–4 hour prototype timebox
as an interactive learning and interview-walkthrough exercise. Before final
submission, replace the placeholders below with your actual approximate time:

- Data model and architecture: `[candidate to provide]`
- Working prototype: `[candidate to provide]`
- Financial controls and compliance analysis: `[candidate to provide]`
- Experience showcase: `[candidate to provide]`

## Project Structure

```text
erp-ar-module/
├── README.md                    Interview entry point and project guide
├── Dockerfile                   FastAPI and worker runtime image
├── docker-compose.yml           Local six-service deployment
├── deploy.sh                    Safe build, migration, startup and verification
├── test_api.sh                  Repeatable API/control integration suite
├── gateway/
│   └── envoy.yaml               Public L7 gateway, JWT, tracing and rate limits
├── database/
│   └── Dockerfile               PostgreSQL 16 image with pg_cron
├── delivery_stub/
│   ├── Dockerfile
│   └── stub.py                  JWKS and invoice-delivery mock
├── migrations/
│   ├── 001_initial_schema.sql
│   ├── 002_add_ar_aging_bucket_counts.sql
│   ├── 003_setup_pg_cron.sh
│   ├── 004_delivery_outbox.sql
│   ├── 005_entity_scoped_idempotency.sql
│   └── 006_fx_rate_ingestion.sql
├── src/
│   ├── main.py                  FastAPI composition and trace middleware
│   ├── auth.py                  JWT/JWKS validation and RBAC
│   ├── database.py              Async SQLAlchemy session setup
│   ├── schemas.py               API request/response contracts
│   ├── seed_data.py             Deterministic demo master data
│   ├── delivery_worker.py       Transactional-outbox delivery consumer
│   ├── fx_rate_worker.py        Scheduled ECB import and INR-rate derivation
│   ├── tests/
│   │   └── test_fx_rate_worker.py  Deterministic ECB parser/derivation tests
│   ├── models/
│   │   ├── __init__.py
│   │   └── models.py            ORM financial and operational models
│   └── routers/
│       ├── invoices.py          Create, retrieve and approve invoices
│       ├── payments.py          Record and allocate receipts
│       ├── aging.py             Customer aging report
│       ├── journal_entries.py   Invoice journal retrieval and pagination
│       └── health.py            Database, MV and AR/GL reconciliation health
└── docs/
    ├── requirements-traceability.md  Living assessment coverage matrix
    ├── high-level-design.md           Component/API architecture
    ├── fx-rate-design.md              Approved multi-currency design
    ├── data-model.md                  ER model and schema decisions
    ├── api-design.md                  Endpoint contracts and errors
    ├── FRs.md / NFRs.md               Functional/non-functional design
    ├── tradeoffs.md                   Decision record
    ├── financial-controls.md          Accounting/SOX/operations analysis
    ├── testing.md / deployment.md     Reproduction and expected results
    └── assessment-submission.md       Consolidated assessment narrative
```

Keep this tree synchronized whenever a service, migration, major module, or
design document is added or removed.
