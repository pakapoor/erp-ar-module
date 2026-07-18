# ERP AR Module
A multi-tenant Invoicing and Accounts Receivable module for mid-market ERP systems.

Built as part of a Principal Engineer technical assessment for DeepRunner.ai.

## Quick Start
```bash
docker-compose up
```

## Tech Stack
- **Backend:** Python / FastAPI
- **Database:** PostgreSQL (with Row Level Security)
- **Containerization:** Docker / Docker Compose
- **AI Tools Used:** Claude (domain learning + design), GitHub Copilot (code)

## Design Walkthrough
For interview/review, follow this order:
- [Functional Requirements](docs/FRs.md)
- [Non Functional Requirements](docs/NFRs.md)
- [Data Model](docs/data-model.md)
- [API Design](docs/api-design.md)
- [Financial Controls](docs/financial-controls.md)
- [Experience Showcase](docs/experience-showcase.md)

## Key Capabilities
- Multi-tenant data isolation (JWT + PostgreSQL RLS)
- Multi-entity support with intercompany elimination
- Multi-currency invoicing with FX gain/loss tracking
- Complete GL journal entry generation
- Invoice lifecycle state machine
- AR aging report
- Idempotent write APIs with cached retry responses
- SOX-compliant audit trail
- Period close controls

## API Endpoints
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /invoices | Create invoice |
| GET | /invoices/{id} | Get invoice with balance |
| POST | /invoices/{id}/approve | Approve invoice + generate GL entries |
| POST | /payments | Record payment + allocate to invoices |
| GET | /customers/{id}/aging | AR aging report |
| GET | /journal-entries | GL entries for invoice |

## Project Structure
</content>
