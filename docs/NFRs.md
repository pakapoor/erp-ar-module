# Non-Functional Requirements — ERP AR Module

## Table of Contents
- [NFR1 — Consistency](#nfr1--consistency)
- [NFR2 — Performance](#nfr2--performance)
- [NFR3 — Availability](#nfr3--availability)
- [NFR4 — Security](#nfr4--security)
- [NFR5 — Observability](#nfr5--observability)
- [NFR6 — Scalability](#nfr6--scalability)
- [NFR7 — Durability](#nfr7--durability)

---

## NFR1 — Consistency

System must prioritize consistency over availability (CP in CAP theorem). All financial operations must be ACID compliant. System returns 503 rather than serve stale or incorrect financial data. Isolation levels selected per operation type.

**Isolation Levels:**
```
Invoice creation:     Read Committed    ← PostgreSQL default, sufficient
Invoice approval:     Repeatable Read   ← consistent view during approval
Payment allocation:   Serializable      ← prevent double allocation
Aging report:         Read Committed    ← read only, 5 min staleness ok
```

**Example:**
```
Two payments hit simultaneously for same invoice:

Without Serializable:
Request 1: reads balance 174,000 → allocates 174,000
Request 2: reads balance 174,000 → allocates 174,000
Result: invoice marked paid TWICE → audit finding!

With Serializable:
Request 1: acquires lock → allocates → commits
Request 2: waits → reads updated balance 0
         → invoice already paid → rejects
Result: correct ✅
```

**Implementation:**
```python
# Payment allocation only — serializable
with db.transaction(isolation='serializable'):
    balance = get_invoice_balance(invoice_id)
    allocate_payment(payment_id, invoice_id, amount)
    generate_journal_entry(...)
    update_invoice_status(...)
```

**Critical Database Constraints:**
```sql
-- Invoice total must equal sum of line items
ALTER TABLE invoices ADD CONSTRAINT check_invoice_total
CHECK (total_amount = subtotal_amount + tax_amount);

-- Journal entries must always balance (debits = credits)
ALTER TABLE journal_entry_lines ADD CONSTRAINT check_je_balance
CHECK (debit_amount = 0 OR credit_amount = 0);

-- Payment allocation cannot exceed invoice balance
ALTER TABLE payment_allocations ADD CONSTRAINT check_allocation
CHECK (amount_allocated <= invoice_balance);

-- Tenant isolation enforced at DB level
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;

-- Invoice status transitions enforced
ALTER TABLE invoices ADD CONSTRAINT check_status
CHECK (status IN ('DRAFT','APPROVED','SENT','PARTIALLY_PAID',
                  'PAID','VOID','WRITTEN_OFF'));
```

### Future Enhancements (Phase 2)
- Real time consistency monitoring
- Automated constraint violation alerts

---

## NFR2 — Performance

API response times must meet defined SLAs. Not all operations require same consistency level — performance targets set accordingly.

**Response Time Targets:**
```
Invoice creation:     < 200ms p99  ← simple write
Invoice retrieval:    < 100ms p99  ← indexed lookup
Invoice approval:     < 300ms p99  ← GL entry generation
Payment allocation:   < 500ms p99  ← serializable transaction
AR Aging report:      < 100ms p99  ← materialized view
Consolidated report:  < 2000ms p99 ← complex multi-entity query
```

**Aging Report — Materialized View Strategy:**
```sql
CREATE MATERIALIZED VIEW ar_aging AS
SELECT
  customer_id,
  tenant_id,
  now() as as_of_timestamp,
  SUM(CASE WHEN days_overdue = 0
      THEN balance END) as current,
  SUM(CASE WHEN days_overdue <= 30
      THEN balance END) as days_30,
  SUM(CASE WHEN days_overdue <= 60
      THEN balance END) as days_60,
  SUM(CASE WHEN days_overdue > 60
      THEN balance END) as days_90_plus
FROM invoices
GROUP BY customer_id, tenant_id;

-- Refreshed every 5 minutes via scheduled job
-- Shows explicit as_of timestamp to user
```

**Example Response:**
```json
{
  "customer_id": "tata-steel",
  "as_of": "2024-01-31 14:35:00",
  "note": "Data refreshed every 5 minutes",
  "buckets": {
    "current": 80000,
    "days_30": 74000,
    "days_60": 0,
    "days_90_plus": 0,
    "total": 154000
  }
}
```

**Key Principle:**
```
Payments:       Serializable    ← strict consistency
Approvals:      Repeatable Read ← strict consistency
Aging report:   5 min stale     ← relaxed, shown explicitly to user
```

### Future Enhancements (Phase 2)
- On-demand aging refresh (manual trigger)
- Per-customer refresh on payment received
- Read replicas for reporting queries

---

## NFR3 — Availability

System must achieve 99.9% uptime. Zero downtime deployments during business hours. Maintenance window 2am-4am tenant local timezone. Consistency guaranteed during failures via atomic transactions and idempotency keys.

**Uptime Targets:**
```
Business hours:     99.9%  (8am-8pm tenant timezone)
Maintenance window: 2am-4am tenant timezone
Annual downtime:    < 8.7 hours
```

**Idempotency — All Write Operations:**
```
POST /invoices
X-Idempotency-Key: "reliance-create-invoice-20240115-001"
→ Duplicate submission → returns same invoice, no duplicate created

POST /invoices/{id}/approve
X-Idempotency-Key: "reliance-approve-1001-20240115"
→ Double approval attempt → returns same response, GL entry not duplicated

POST /payments
X-Idempotency-Key: "tata-steel-P001-20240131"
→ Duplicate payment → returns same response, payment not duplicated
```

**Idempotency Implementation:**
```
Client sends:
POST /payments
X-Idempotency-Key: "tata-steel-P001-20240131"

Server:
Step 0: Check idempotency_keys table
→ Key COMPLETED   → return cached response (no reprocess)
→ Key PROCESSING  → return 409 "in progress"
→ Key not found   → insert key with PROCESSING, proceed

Retry scenario:
Request 1: key not found → process → mark COMPLETED
Request 2: key found + COMPLETED → same response ✅
No duplicate payment! ✅
```

**Atomic Transaction — Crash Safety:**
```
BEGIN TRANSACTION
  Step 0: Check + insert idempotency key
  Step 1: Record payment
  Step 2: Allocate to invoices
  Step 3: Generate GL journal entry
  Step 4: Update invoice status
  Step 5: Mark idempotency key COMPLETED
COMMIT

Server crashes at Step 4:
→ ROLLBACK everything ✅
→ Clean state ✅
→ Client retries safely ✅
→ Idempotency key not COMPLETED → reprocessed safely ✅
```

**Infrastructure:**
```
Multiple API instances (load balanced)
PostgreSQL primary + read replica
Automatic failover < 30 seconds
Health checks + auto restart
```

### Future Enhancements (Phase 2)
- Active-passive multi-region (99.99% enterprise SLA)
- Automated failover < 60 seconds
- Saga pattern if microservices introduced

---

## NFR4 — Security

System must protect data in transit and at rest. Access controlled via RBAC (FR16) and tenant isolation (FR10). PII fields encrypted at column level. All communication over HTTPS. Security implemented via managed services — complexity owned by platform, not application code.

**In Transit:**
```
HTTPS/TLS 1.3 for all API calls
Services communicate within VPC (not public internet)
API Gateway handles SSL termination
```

**At Rest:**
```
PostgreSQL disk encryption (AWS RDS default)
PII column encryption via AWS KMS:
  - customer.tax_identifier  ← GST/PAN
  - customer.bank_details    ← payment info
  
Hybrid encryption:
  AES → encrypts actual data (fast, handles any size)
  RSA → encrypts AES key (via KMS/HSM, tamper-proof)

Who can decrypt?
  App service account  ✅ (to serve API requests)
  DBA                  ❌ (sees ciphertext only)
  AWS support          ❌ (HSM — physically tamper proof)
  Auditor              ✅ (read-only IAM role)
```

**Example:**
```
DBA runs: SELECT * FROM customers;

Result:
id        | name       | tax_identifier
----------|------------|---------------------
tata-001  | Tata Steel | xK9mP2qR7nL4...  ← ciphertext!

App decrypts via KMS:
tax_identifier → "29AAACT1234F1Z5"  ← only app sees plaintext
```

**Access Control:**
```
DB access via service accounts (least privilege)
No direct DB access for humans in production
Root DB credentials in AWS Secrets Manager
All human access via application layer only
```

**Enterprise Tier — BYOK:**
```
Bring Your Own Key:
Tenant generates RSA keypair
Tenant gives app permission to use public key
Tenant holds private key
Tenant can revoke anytime → instant data inaccessibility
Full data sovereignty ✅
```

### Future Enhancements (Phase 2)
- Penetration testing + VAPT
- SOC2 certification
- IP whitelisting per tenant
- MFA for CFO and admin roles

---

## NFR5 — Observability

System must provide full observability across infrastructure, business, and financial integrity dimensions. OpenTelemetry for instrumentation, DataDog for visualization, PagerDuty for critical alerts.

**Infrastructure Metrics:**
```
- Server CPU, memory, disk utilization
- DB connection pool usage
- DB query latency (p50, p95, p99)
- API Gateway request rate
- Kafka consumer lag (audit trail pipeline)
```

**Business Metrics:**
```
- API response times per endpoint (p50, p95, p99)
- API success/failure rates per endpoint
- Payment processing success rate
- Invoice creation/approval rates
- Daily transaction volumes per tenant
```

**Financial Integrity Metrics:**
```
- AR subledger vs GL mismatch count    ← must always be 0!
- Time since last successful reconciliation
- Duplicate payment attempts (idempotency hits)
- Unallocated payments (payment received, no invoice matched)
- Invoices stuck in Draft > 7 days     ← forgotten invoices!
- Invoices overdue > 90 days           ← write-off candidates
- Unbalanced journal entries           ← must never happen!
- Days since period close              ← is Feb still open in April?
```

**APM — Distributed Tracing:**
```
POST /payments → trace_id: abc123
  ├── JWT validation          2ms
  ├── Idempotency check       5ms
  ├── DB: check balance       10ms
  ├── DB: allocate payment    45ms  ← slow! investigate
  ├── DB: generate GL entry   8ms
  └── DB: update status       5ms
Total: 75ms
```

**Alerting Tiers:**
```
RED (PagerDuty — immediate page):
  - AR/GL mismatch detected
  - Unbalanced journal entry created
  - Payment failure rate > 1%
  - DB primary down

YELLOW (Slack — batched summary):
  - Invoice stuck in Draft > 7 days
  - Reconciliation not run > 24hrs
  - API p99 > 500ms
  - Exchange rate not updated > 24hrs

GREEN (Dashboard only):
  - Normal business metrics
  - Daily transaction volumes
  - Tenant usage stats
```

**Tech Stack:**
```
OpenTelemetry  ← instrumentation (vendor neutral, no lock-in)
DataDog        ← visualization + alerting
PagerDuty      ← RED alert escalation
```

**Lenovo IoT Parallel:**
```
IoT:  Edge → Kafka → Flink → SQS → ServiceNow
      Each hop traced with correlation ID
      DataDog + PagerDuty for alerts

ERP:  API Gateway → FastAPI → PostgreSQL
      Each hop traced with trace_id
      Same DataDog + PagerDuty stack ✅
```

### Future Enhancements (Phase 2)
- Anomaly detection on financial metrics
- ML-based fraud detection alerts
- Real time reconciliation dashboard
- Tenant-level SLA dashboards

---

## NFR6 — Scalability

System must scale horizontally to support mid-market SaaS workloads. API layer scales independently of database layer. Bulk operations handled asynchronously to avoid timeout.

**Scale Targets:**
```
Tenants:            100+
Concurrent users:   10,000 (100 tenants × 100 users)
Invoices/month:     1M (100 tenants × 10,000 invoices)
Payments/day:       50,000
Bulk invoice import: 1,000 invoices per batch
Bulk payment import: 10,000 payments per batch
```

**API Layer Scaling:**
```
Horizontal scaling via load balancer
Stateless API servers (no session state)
JWT carries all context (tenant_id, entity_id, roles)
Add/remove servers without downtime
```

**Database Scaling:**
```
Primary      → all writes
Read Replica → aging reports, consolidated reports
Connection pooling via PgBouncer
Table partitioning by tenant_id:

invoices partitioned by tenant_id:
├── invoices_reliance
├── invoices_tata
└── invoices_jsw
```

**Bulk Operations — Async (Invoices):**
```
POST /invoices/bulk
Request:  1,000 invoices
Response: 202 Accepted
{
  "job_id": "bulk-inv-001",
  "status": "processing"
}

GET /jobs/bulk-inv-001
{
  "status": "completed",
  "processed": 998,
  "failed": 2,
  "errors": [
    {"line": 5, "error": "Customer not found"},
    {"line": 10, "error": "Invalid tax jurisdiction"}
  ]
}
```

**Bulk Operations — Async (Payments):**
```
POST /payments/bulk
Request:  10,000 payments
Response: 202 Accepted
{
  "job_id": "bulk-pay-001",
  "status": "processing"
}

GET /jobs/bulk-pay-001
{
  "status": "completed",
  "processed": 9998,
  "failed": 2,
  "errors": [...]
}
```

**Example — Without vs With Async:**
```
10,000 payments arrive simultaneously:

Without async:
→ 10,000 requests timeout ❌
→ Clients retry → 20,000 requests 😱
→ System overload

With async bulk + queue:
→ One bulk request accepted immediately ✅
→ Queue processes at safe rate
→ Client polls job status
→ No timeouts, no retries needed ✅
```

### Future Enhancements (Phase 2)
- Multi-region sharding by tenant
- Kafka for payment event streaming
- CQRS for reporting queries
- Auto-scaling based on load metrics

---

## NFR7 — Durability

Financial data must never be lost. 7 year retention for SOX compliance. RTO and RPO defined per criticality. Backups automated and tested regularly.

**Recovery Targets:**
```
RPO (Recovery Point Objective):
→ Maximum data loss acceptable
→ Financial system: RPO = 0  (zero data loss!)
→ Achieved via: synchronous replication to standby

RTO (Recovery Time Objective):
→ Maximum downtime acceptable
→ Financial system: RTO = 30 minutes
→ Achieved via: automated failover
```

**Backup Strategy:**
```
Continuous WAL archival → S3 (every transaction!)
Daily snapshots        → S3 (point in time recovery)
Weekly full backup     → S3 Glacier (cheap long term)
7 year retention       → S3 WORM (SOX compliance, immutable)
```

**Data Retention Tiers:**
```
Hot  (0-6 months):   PostgreSQL    ← fast queries, recent data
Warm (6-24 months):  S3 Standard   ← occasional access
Cold (2-7 years):    S3 Glacier    ← compliance archival only
```

**Zero Data Loss — Journal Entries (3 copies):**
```
Journal entries → Kafka         (durable log, replay capable)
               → PostgreSQL     (queryable, fast access)
               → S3 WORM        (immutable, 7 years, SOX)

Three copies. SOX auditor happy! ✅
```

**Example — Crash Recovery:**
```
DB primary crashes at 2:00pm:

RPO = 0:
→ Standby has ALL transactions via sync replication
→ Zero data loss ✅

RTO = 30 mins:
→ Automated failover to standby
→ DNS updated automatically
→ App reconnects via connection pooler
→ Back online by 2:30pm ✅
→ Users see brief 503, retry succeeds ✅
```

**99.99% SLA Consideration:**
```
99.99% = 52 minutes downtime/year
Requires: Active-passive multi-region
Tradeoff: Consistency risk during failover
          Additional cost + complexity
Recommendation: 99.9% for standard tier
                99.99% for enterprise tier only
                (explicit cost + complexity tradeoff)
```

### Future Enhancements (Phase 2)
- Active-passive multi-region (99.99% enterprise SLA)
- Automated backup restore drills (test recovery monthly)
- Point in time recovery UI for ops team
