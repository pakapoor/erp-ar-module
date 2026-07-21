# Non-Functional Requirements ‚Ä" ERP AR Module

## Table of Contents
- [NFR1 ‚Ä" Consistency](#nfr1--consistency)
- [NFR2 ‚Ä" Performance](#nfr2--performance)
- [NFR3 ‚Ä" Availability](#nfr3--availability)
- [NFR4 ‚Ä" Security](#nfr4--security)
- [NFR5 ‚Ä" Observability](#nfr5--observability)
- [NFR6 ‚Ä" Scalability](#nfr6--scalability)
- [NFR7 ‚Ä" Durability](#nfr7--durability)

---

## NFR1 ‚Ä" Consistency

System must prioritize consistency over availability (CP in CAP theorem). All financial operations must be ACID compliant. System returns 503 rather than serve stale or incorrect financial data. Isolation levels selected per operation type.

**Isolation Levels:**
```
Invoice creation:     Read Committed    ‚Üê PostgreSQL default, sufficient
Invoice approval:     Repeatable Read   ‚Üê consistent view during approval
Payment allocation:   Serializable      ‚Üê prevent double allocation
Aging report:         Read Committed    ‚Üê read only, 5 min staleness ok
```

**Example:**
```
Two payments hit simultaneously for same invoice:

Without Serializable:
Request 1: reads balance 174,000 ‚Ü' allocates 174,000
Request 2: reads balance 174,000 ‚Ü' allocates 174,000
Result: invoice marked paid TWICE ‚Ü' audit finding!

With Serializable:
Request 1: acquires lock ‚Ü' allocates ‚Ü' commits
Request 2: waits ‚Ü' PostgreSQL detects its stale serializable snapshot
         ‚Ü' transaction aborts with SQLSTATE 40001
         ‚Ü' API returns retryable 409 PAYMENT_CONCURRENCY_CONFLICT
Result: correct ‚úÖ
```

**Implementation:**
```python
# Payment allocation only ‚Ä" serializable
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

## NFR2 ‚Ä" Performance

API response times must meet defined SLAs. Not all operations require same consistency level ‚Ä" performance targets set accordingly.

**Response Time Targets:**
```
Invoice creation:     < 200ms p99  ‚Üê simple write
Invoice retrieval:    < 100ms p99  ‚Üê indexed lookup
Invoice approval:     < 300ms p99  ‚Üê GL entry generation
Payment allocation:   < 500ms p99  ‚Üê serializable transaction
AR Aging report:      < 100ms p99  ‚Üê materialized view
Consolidated report:  < 2000ms p99 ‚Üê complex multi-entity query
```

**Aging Report ‚Ä" Materialized View Strategy:**
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
-- Shows explicit refresh-time as_of timestamp to user
-- Current snapshot only; it does not answer historical as_of queries
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
Payments:       Serializable    ‚Üê strict consistency
Approvals:      Repeatable Read ‚Üê strict consistency
Aging report:   5 min stale     ‚Üê relaxed, shown explicitly to user
```

### Future Enhancements (Phase 2)
- On-demand aging refresh (manual trigger)
- Per-customer refresh on payment received
- Read replicas for reporting queries
- Historical aging reconstructed from dated financial events or daily snapshots

---

## NFR3 ‚Ä" Availability

System must achieve 99.9% uptime. Zero downtime deployments during business hours. Maintenance window 2am-4am tenant local timezone. Consistency guaranteed during failures via atomic transactions and idempotency keys.

**Uptime Targets:**
```
Business hours:     99.9%  (8am-8pm tenant timezone)
Maintenance window: 2am-4am tenant timezone
Annual downtime:    < 8.7 hours
```

**Idempotency ‚Ä" All Write Operations:**
```
POST /invoices
X-Idempotency-Key: "reliance-create-invoice-20240115-001"
‚Ü' Duplicate submission ‚Ü' returns same invoice, no duplicate created

POST /invoices/{id}/approve
X-Idempotency-Key: "reliance-approve-1001-20240115"
‚Ü' Double approval attempt ‚Ü' returns same response, GL entry not duplicated

POST /payments
X-Idempotency-Key: "tata-steel-P001-20240131"
‚Ü' Duplicate payment ‚Ü' returns same response, payment not duplicated
```

**Idempotency Implementation:**
```
Client sends:
POST /payments
X-Idempotency-Key: "tata-steel-P001-20240131"

Server:
Step 0: Canonicalize the request and calculate its SHA-256 request_hash
Step 1: Check idempotency_key by (tenant_id, entity_id, endpoint, key)
‚Ü' Key COMPLETED + same hash  ‚Ü' replay cached HTTP status and body
‚Ü' Key PROCESSING + same hash ‚Ü' return 409 REQUEST_IN_PROGRESS
‚Ü' Same key + different hash  ‚Ü' return 409 IDEMPOTENCY_KEY_REUSED
‚Ü' Key not found              ‚Ü' insert PROCESSING and proceed

Retry scenario:
Request 1: key not found ‚Ü' process ‚Ü' mark COMPLETED
Request 2: key found + COMPLETED ‚Ü' same response ‚úÖ
No duplicate payment! ‚úÖ
```

**Atomic Transaction ‚Ä" Crash Safety:**
```
BEGIN TRANSACTION
  Step 0: Claim unique (tenant_id, entity_id, endpoint, key) with request_hash
  Step 1: Record payment
  Step 2: Allocate to invoices
  Step 3: Generate GL journal entry
  Step 4: Update invoice status
  Step 5: Mark idempotency key COMPLETED
COMMIT

Server crashes at Step 4:
‚Ü' ROLLBACK everything ‚úÖ
‚Ü' Clean state ‚úÖ
‚Ü' Client retries safely ‚úÖ
‚Ü' Idempotency key not COMPLETED ‚Ü' reprocessed safely ‚úÖ
```

The idempotency claim and financial writes share one transaction. PostgreSQL
serialization/deadlock failures (`40001`/`40P01`) become
`409 PAYMENT_CONCURRENCY_CONFLICT`; the client retries with the same
idempotency key. The aborted transaction leaves no payment, allocation,
journal, balance update or idempotency claim behind.

The repeatable concurrency suite races two distinct full payments in both AUTO
and MANUAL modes. Each race proves one HTTP 201, one retryable HTTP 409, exactly
one allocation and payment journal, one payment-driven version increment, zero
balance and healthy AR/GL reconciliation. The test first lets the asynchronous
delivery transition settle, so the expected lifecycle is DRAFT v1, APPROVED v2,
SENT v3 and PAID v4.

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

## NFR4 ‚Ä" Security

System must protect data in transit and at rest. Access is controlled via RBAC
(FR14) and tenant isolation (FR10). PII fields are encrypted at column level.
All communication uses HTTPS in production. Security is implemented through
managed services so platform components own key and certificate complexity.

**In Transit:**
```
HTTPS/TLS 1.3 for all API calls
Services communicate within VPC (not public internet)
Envoy handles TLS termination in production. The local Docker demo intentionally
uses HTTP on localhost; this is not a production security posture.
```

**At Rest:**
```
PostgreSQL disk encryption (AWS RDS default)
PII column encryption via AWS KMS:
  - customer.tax_identifier  ‚Üê GST/PAN
  - customer.bank_details    ‚Üê payment info
  
Hybrid encryption:
  AES ‚Ü' encrypts actual data (fast, handles any size)
  RSA ‚Ü' encrypts AES key (via KMS/HSM, tamper-proof)

Who can decrypt‚Ü'
  App service account  ‚úÖ (to serve API requests)
  DBA                  ‚ùå (sees ciphertext only)
  AWS support          ‚ùå (HSM ‚Ä" physically tamper proof)
  Auditor              ‚úÖ (read-only IAM role)
```

**Example:**
```
DBA runs: SELECT * FROM customers;

Result:
id        | name       | tax_identifier
----------|------------|---------------------
tata-001  | Tata Steel | xK9mP2qR7nL4...  ‚Üê ciphertext!

App decrypts via KMS:
tax_identifier ‚Ü' "29AAACT1234F1Z5"  ‚Üê only app sees plaintext
```

**Access Control:**
```
DB access via service accounts (least privilege)
No direct DB access for humans in production
Root DB credentials in AWS Secrets Manager
All human access via application layer only
```

**Enterprise Tier ‚Ä" BYOK:**
```
Bring Your Own Key:
Tenant generates RSA keypair
Tenant gives app permission to use public key
Tenant holds private key
Tenant can revoke anytime ‚Ü' instant data inaccessibility
Full data sovereignty ‚úÖ
```

### Future Enhancements (Phase 2)
- Penetration testing + VAPT
- SOC2 certification
- IP whitelisting per tenant
- MFA for CFO and admin roles

---

## NFR5 ‚Ä" Observability

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
- AR subledger vs GL mismatch count    ‚Üê must always be 0!
- Time since last successful reconciliation
- Duplicate payment attempts (idempotency hits)
- Unallocated payments (payment received, no invoice matched)
- Invoices stuck in Draft > 7 days     ‚Üê forgotten invoices!
- Invoices overdue > 90 days           ‚Üê write-off candidates
- Unbalanced journal entries           ‚Üê must never happen!
- Days since period close              ‚Üê is Feb still open in April‚Ü'
```

**APM ‚Ä" Distributed Tracing:**
```
POST /payments ‚Ü' trace_id: abc123
  ‚"ú‚"Ä‚"Ä JWT validation          2ms
  ‚"ú‚"Ä‚"Ä Idempotency check       5ms
  ‚"ú‚"Ä‚"Ä DB: check balance       10ms
  ‚"ú‚"Ä‚"Ä DB: allocate payment    45ms  ‚Üê slow! investigate
  ‚"ú‚"Ä‚"Ä DB: generate GL entry   8ms
  ‚""‚"Ä‚"Ä DB: update status       5ms
Total: 75ms
```

**Alerting Tiers:**
```
RED (PagerDuty ‚Ä" immediate page):
  - AR/GL mismatch detected
  - Unbalanced journal entry created
  - Payment failure rate > 1%
  - DB primary down

YELLOW (Slack ‚Ä" batched summary):
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
OpenTelemetry  ‚Üê instrumentation (vendor neutral, no lock-in)
DataDog        ‚Üê visualization + alerting
PagerDuty      ‚Üê RED alert escalation
```

**Lenovo IoT Parallel:**
```
IoT:  Edge ‚Ü' Kafka ‚Ü' Flink ‚Ü' SQS ‚Ü' ServiceNow
      Each hop traced with correlation ID
      DataDog + PagerDuty for alerts

ERP:  Envoy L7 Gateway ‚Ü' FastAPI (internal network only) ‚Ü' PostgreSQL
      Each hop traced with trace_id
      Same DataDog + PagerDuty stack ‚úÖ
```

### Future Enhancements (Phase 2)
- Anomaly detection on financial metrics
- ML-based fraud detection alerts
- Real time reconciliation dashboard
- Tenant-level SLA dashboards

---

## NFR6 ‚Ä" Scalability

System must scale horizontally to support mid-market SaaS workloads. API layer scales independently of database layer. Bulk operations handled asynchronously to avoid timeout.

**Scale Targets:**
```
Tenants:            100+
Concurrent users:   10,000 (100 tenants √-- 100 users)
Invoices/month:     1M (100 tenants √-- 10,000 invoices)
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
Primary      ‚Ü' all writes
Read Replica ‚Ü' aging reports, consolidated reports
Connection pooling via PgBouncer
Table partitioning by tenant_id:

invoices partitioned by tenant_id:
‚"ú‚"Ä‚"Ä invoices_reliance
‚"ú‚"Ä‚"Ä invoices_tata
‚""‚"Ä‚"Ä invoices_jsw
```

**Bulk Operations ‚Ä" Async (Invoices):**
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

**Bulk Operations ‚Ä" Async (Payments):**
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

**Example ‚Ä" Without vs With Async:**
```
10,000 payments arrive simultaneously:

Without async:
‚Ü' 10,000 requests timeout ‚ùå
‚Ü' Clients retry ‚Ü' 20,000 requests üò±
‚Ü' System overload

With async bulk + queue:
‚Ü' One bulk request accepted immediately ‚úÖ
‚Ü' Queue processes at safe rate
‚Ü' Client polls job status
‚Ü' No timeouts, no retries needed ‚úÖ
```

### Future Enhancements (Phase 2)
- Multi-region sharding by tenant
- Kafka for payment event streaming
- CQRS for reporting queries
- Auto-scaling based on load metrics

### FX Provider Isolation and Scheduling

Financial request latency and availability must not depend on a synchronous
market-data call. pg_cron inserts one weekday `fx_import_job`; a separately
scalable worker claims it with `FOR UPDATE SKIP LOCKED`, calls the ECB API with
bounded timeouts/retries, validates the batch, and stores approved rates.

Invoice/payment APIs read PostgreSQL only. They fail closed when no approved
rate exists within three calendar days. Deterministic integration tests use a
recorded ECB-shaped response; a separate optional contract smoke test detects
live provider changes without making the main suite flaky. Monitoring covers
provider-date freshness, import lag, retries and DEAD jobs. See
[FX Rate Ingestion and Multi-Currency Design](fx-rate-design.md).

### Delivery Queue Scaling and Failure Isolation

Invoice approval writes one durable event in its PostgreSQL transaction; it
never waits for SQS or the delivery adapter. Publishers claim independent rows
with `FOR UPDATE SKIP LOCKED`, while Standard SQS buffers bursts and lets
delivery consumers scale separately. Consumers delete only after the adapter
success and DB status commit. Because Standard SQS is at-least-once, both the
DB event claim and the adapter deduplicate using the stable outbox event ID.

Three failed receives redrive to a DLQ. The CFO retry API resets only a DEAD
event to PENDING, after which the publisher performs the broker call. Production
adds DLQ-depth/oldest-age alarms, retention/archival, bounded bulk redrive and
autoscaling on queue age‚Ä"not on financial API latency.

---

## NFR7 ‚Ä" Durability

Financial data must never be lost. 7 year retention for SOX compliance. RTO and RPO defined per criticality. Backups automated and tested regularly.

**Recovery Targets:**
```
RPO (Recovery Point Objective):
‚Ü' Maximum data loss acceptable
‚Ü' Financial system: RPO = 0  (zero data loss!)
‚Ü' Achieved via: synchronous replication to standby

RTO (Recovery Time Objective):
‚Ü' Maximum downtime acceptable
‚Ü' Financial system: RTO = 30 minutes
‚Ü' Achieved via: automated failover
```

**Backup Strategy:**
```
Continuous WAL archival ‚Ü' S3 (every transaction!)
Daily snapshots        ‚Ü' S3 (point in time recovery)
Weekly full backup     ‚Ü' S3 Glacier (cheap long term)
7 year retention       ‚Ü' S3 WORM (SOX compliance, immutable)
```

**Data Retention Tiers:**
```
Hot  (0-6 months):   PostgreSQL    ‚Üê fast queries, recent data
Warm (6-24 months):  S3 Standard   ‚Üê occasional access
Cold (2-7 years):    S3 Glacier    ‚Üê compliance archival only
```

**Zero Data Loss ‚Ä" Journal Entries (3 copies):**
```
Journal entries ‚Ü' Kafka         (durable log, replay capable)
               ‚Ü' PostgreSQL     (queryable, fast access)
               ‚Ü' S3 WORM        (immutable, 7 years, SOX)

Three copies. SOX auditor happy! ‚úÖ
```

**Example ‚Ä" Crash Recovery:**
```
DB primary crashes at 2:00pm:

RPO = 0:
‚Ü' Standby has ALL transactions via sync replication
‚Ü' Zero data loss ‚úÖ

RTO = 30 mins:
‚Ü' Automated failover to standby
‚Ü' DNS updated automatically
‚Ü' App reconnects via connection pooler
‚Ü' Back online by 2:30pm ‚úÖ
‚Ü' Users see brief 503, retry succeeds ‚úÖ
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
