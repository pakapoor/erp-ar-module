# Assessment Requirements Traceability

This is the living source of truth for assessment coverage. Update it whenever
a requirement is implemented, tested, deliberately deferred, or re-scoped.
It follows the original assessment rather than treating every idea in
`FRs.md`/`NFRs.md` as mandatory prototype scope.

## Status Legend

| Status | Meaning |
|---|---|
| `COMPLETE` | Required design/documentation exists and the claimed prototype behavior is implemented and tested |
| `DESIGN COMPLETE` | The assessment asks for an approach/design; it is documented, but no prototype implementation is claimed |
| `IN PROGRESS` | We decided to implement it and work remains |
| `PARTIAL` | Some required or claimed behavior exists, but an important gap remains |
| `V2` | Deliberately deferred after review; the V2 design/acceptance criteria must remain documented |
| `USER INPUT` | Cannot be completed truthfully without Pankaj's personal information |
| `NOT PLANNED` | Optional bonus deliberately excluded, with an honest rationale |

## A. Working Prototype -- Explicitly Required

These are the strongest submission obligations: the assessment explicitly
labels them required prototype endpoints/functionality.

| ID | Assessment requirement | Design | Prototype/test | Status | Evidence / remaining action |
|---|---|---|---|---|---|
| WP1 | `POST /invoices` with line items | Complete | Implemented and integration-tested | `COMPLETE` | `src/routers/invoices.py`, `tests/integration/test_api.sh` |
| WP2 | `GET /invoices/{id}` with balance and payment history | Complete | Implemented and integration-tested | `COMPLETE` | Includes line items, payment history, credit-memo history, status history and ETag |
| WP3 | `POST /invoices/{id}/approve` plus GL entries | Complete | Implemented and integration/concurrency-tested | `COMPLETE` | Atomic approval, GL, idempotency and delivery-outbox write; SQS publication is after commit |
| WP4 | `POST /payments` allocated to one or more invoices | Complete | AUTO FIFO and MANUAL allocation tested | `COMPLETE` | Partial payment, idempotent retry and overpayment liability are tested |
| WP5 | `GET /customers/{id}/aging` with current/30/60/90+ | Complete | Implemented and tested | `COMPLETE` | PostgreSQL MV, pg_cron refresh and freshness timestamp |
| WP6 | `GET /journal-entries?invoice={id}` | Complete | Implemented and pagination-tested | `COMPLETE` | Balanced entries and invoice-wide AR summary verified |
| WP7 | Multi-tenant isolation | Complete | Tenant and entity denial tests pass | `COMPLETE` | JWT-derived scope plus application filters; production RLS hardening tracked under NFR3 |
| WP8 | Validated invoice state transitions | Complete | Core create/approve/send/pay path tested | `COMPLETE` | Bonus void/write-off routes exist but remain outside this required, tested claim. `PATCH /invoices/{id}` and the reject branch (`action: REJECT` on approve) are covered by `tests/unit/test_invoice_patch_reject.py` (11 tests: guard clauses, SOX, version conflicts, happy paths) and `tests/integration/test_api.sh` (API3b: reject -> patch -> re-approve, role checks, idempotency replay) as of 2026-07-22 |
| WP9 | Automatic GL entry generation on approval | Complete | Balanced journal tested | `COMPLETE` | AR, Revenue and Tax Payable lines |
| WP10 | FIFO or manual payment allocation | Complete | Both modes implemented; AUTO path tested in walkthrough | `COMPLETE` | MANUAL also exercised by overpayment control |
| WP11 | Basic audit logging | Complete | DB triggers active during tested writes | `COMPLETE` | Actor and old/new row data captured; immutability hardening is NFR4 |

## B. Data Model and Architecture -- Explicitly Required Design

| ID | Assessment requirement | Design | Prototype/test | Status | Evidence / remaining action |
|---|---|---|---|---|---|
| DA1 | ER model with Customer, Invoice, Line Item, Payment, Credit Memo, GL Account and Journal Entry | Complete | Tables/models exist | `COMPLETE` | `docs/data-model.md`, ER diagrams, migration 001 |
| DA2 | Tenant isolation with multi-entity/subsidiary structure | Complete | Same-tenant sibling-entity isolation tested | `COMPLETE` | Tenant -- Entity model and entity-scoped idempotency |
| DA3 | Base vs transaction currency and exchange-rate storage | Complete | Live ECB ingestion plus invoice, approval and payment snapshots/journals tested | `COMPLETE` | Locked immutable rate IDs; never silently default missing FX rates to 1.0 |
| DA4 | Audit trail: who changed what and when | Complete | DB triggers implemented | `COMPLETE` | Application sets transaction-local actor context |
| DA5 | Invoice-to-GL accounting | Complete | Implemented/tested | `COMPLETE` | Approval journal balances |
| DA6 | Payment recording and allocation | Complete | Implemented/tested | `COMPLETE` | Payment, allocation, invoice and journal commit atomically |
| DA7 | Partial payments and overpayments | Complete | Implemented/tested | `COMPLETE` | Unapplied amount credits Customer Credit liability, not AR |
| DA8 | Credit memo application and write-off procedures | Complete | Credit memo extended matrix complete; write-off INR happy path passes | `PARTIAL` | Complete write-off entity/FX/concurrency/idempotency/direct-journal tests |
| DA9 | Lifecycle states and transition rules | Complete | Core path tested; bonus terminal paths implemented | `COMPLETE` | DRAFT, APPROVED, SENT, PARTIALLY_PAID and PAID tested; VOID/WRITTEN_OFF verification tracked under B6--B8 |
| DA10 | Operations allowed in each state | Complete | Core write guards implemented | `COMPLETE` | Approved invoices are not edited in place; amendment paths are documented. The DRAFT-only edit guard on `PATCH /invoices/{id}` (and the reject-to-DRAFT transition) is now tested -- see WP8 |
| DA11 | Key API request/response contracts | Complete | Six required APIs implemented | `COMPLETE` | `docs/api-design.md` |
| DA12 | Payment idempotency approach | Complete | Implemented/tested | `COMPLETE` | Entity-scoped key, request hash, cached response and payment-reference uniqueness |
| DA13 | Batch invoicing/bulk payment approach | Complete | No bulk API required | `DESIGN COMPLETE` | Async bounded jobs, per-item idempotency and retry are documented as V2 |

## C. Financial Controls and Operational Analysis -- Explicitly Required Design

| ID | Assessment requirement | Design | Prototype/test | Status | Evidence / remaining action |
|---|---|---|---|---|---|
| FC1 | Invoice/payment balance and AR-to-GL reconciliation | Complete | Automated health reconciliation tested | `COMPLETE` | Required analysis plus bonus reconciliation implementation |
| FC2 | Duplicate-payment prevention | Complete | Multiple controls tested | `COMPLETE` | Idempotency, request hash and unique payment reference |
| FC3 | Critical DB constraints and application validation | Complete | Core controls exercised | `COMPLETE` | Decimal server calculations, checks, uniqueness and period trigger |
| FC4 | SOX audit requirements | Complete | Basic audit implemented | `DESIGN COMPLETE` | Immutable archival/privilege enforcement remains production NFR4 |
| FC5 | Approved-invoice amendments | Complete | Bonus credit/void routes implemented; acceptance pending | `DESIGN COMPLETE` | Required decision is documented; implementation proof remains B6/B7 |
| FC6 | Segregation of duties | Complete | Creator/approver denial tested | `COMPLETE` | Separate creator, approver/CFO and payment-recorder roles |
| FC7 | Month/year-end close | Complete | Posting check implemented; management APIs deferred | `DESIGN COMPLETE` | OPEN/CLOSED/LOCKED rules and controls documented |
| FC8 | Posting an invoice to a closed period | Complete | App plus DB enforcement implemented | `COMPLETE` | Returns locked-period error and creates no journal |
| FC9 | Prior-period corrections | Complete | Schema supports metadata; workflow API deferred | `DESIGN COMPLETE` | Current-period adjustment with original document date and approval |
| FC10 | Zero-data-loss deployment strategy | Complete | Safe local deploy script exists | `DESIGN COMPLETE` | Production expand/migrate/contract and rolling/blue-green approach documented |
| FC11 | Backup and recovery | Complete | Not provisioned in local prototype | `DESIGN COMPLETE` | Encrypted snapshots, WAL/PITR, restore drills and reconciliation documented |
| FC12 | Failure during payment transaction | Complete | Atomicity/idempotent retry tested indirectly | `COMPLETE` | Rollback before commit; cached response after commit |

## D. Scenario Capabilities and Bonus Implementation

The scenario motivates the architecture, while the assessment's bonus section
clarifies which deeper implementations are optional. We decide these one by
one instead of silently expanding scope.

| ID | Capability | Current position | Status | Decision / next action |
|---|---|---|---|---|
| B1 | Full multi-currency and exchange-rate handling | ECB ingestion, dual-currency posting and realized gain/loss implemented/tested | `COMPLETE` | Gain, loss, partial/final payment, stale rate and mixed-currency controls pass |
| B2 | Intercompany invoicing and elimination | Separate consolidation-ledger design documented; invoice schema has preparatory flags | `V2` | Match legal-entity entries, post audited elimination batches, and reconcile mismatches without rewriting entity books |
| B3 | Revenue recognition schedules/deferred revenue | Point-in-time scope and deferred-revenue approach documented | `V2` | Over-time lines credit Deferred Revenue and use persisted schedules; Finance policy approval precedes implementation |
| B4 | Odoo module or SAP integration patterns | FastAPI solution chosen | `NOT PLANNED` | Optional bonus; unrelated rewrite would weaken the submission |
| B5 | Automated AR subledger-to-GL reconciliation | Health reconciliation implemented/tested | `COMPLETE` | Already earns the bonus at prototype scale |
| B6 | Credit memo workflow | Entity/FX/paid-and-partial/concurrency/idempotency/API6 journal controls pass | `COMPLETE` | Row locking caps cumulative reversals; paid amounts become Customer Credit liability instead of negative AR |
| B7 | Void/reissue workflow | DRAFT void tested; reissue absent | `IN PROGRESS` | Verify posted reversal; keep reissue in V2 unless selected |
| B8 | Write-off workflow | CFO INR write-off and reconciliation pass | `IN PROGRESS` | Verify partial-payment, entity/FX/concurrency/idempotency/direct journal controls |
| B9 | Period-management and manual-adjustment APIs | Schema/control design only | `V2` | Review after required/design gaps |

## E. NFR / Production-Hardening Backlog

These do not invalidate the functional prototype. They must remain visible so
the interview story separates prototype correctness from production readiness.

| ID | NFR/control | Current state | Status | V2 acceptance target |
|---|---|---|---|---|
| NFR1 | Full payment concurrency race matrix | AUTO and MANUAL full-balance races prove one 201/one retryable 409 and one posting | `COMPLETE` | `tests/concurrency/test_payment_concurrency.sh`; reconciliation remains matched |
| NFR2 | Aggregate journal balancing at database boundary | Application creates balanced journals; line constraints exist | `V2` | Deferred DB constraint trigger rejects an unbalanced posted journal |
| NFR3 | Defense-in-depth PostgreSQL RLS | Policies exist but owner runtime can bypass them | `V2` | Non-owner runtime role, `FORCE ROW LEVEL SECURITY`, negative DB tests |
| NFR4 | Immutable audit and posted journals | Audit triggers exist; physical UPDATE/DELETE denial is not enforced | `V2` | Separate privileges/triggers plus retention and tamper monitoring |
| NFR-B1 | Production JWT/TLS | Dual JWT verification works locally over HTTP | `V2` | TLS at Envoy, required issuer/audience, asymmetric keys and rotation |
| NFR-B2 | Distributed per-tenant rate limiting | Envoy local per-instance bucket | `V2` | Shared rate-limit service with tenant descriptors and tests |
| NFR-B3 | Outbox operations | Standard SQS/DLQ, status API, CFO replay and downstream deduplication implemented | `PARTIAL` | Focused happy/failure tests exist; production still needs DLQ alarms, retention, metrics and bulk redrive |
| NFR-B1 | Managed backup/restore proof | Strategy documented | `V2` | PITR configuration, measured RPO/RTO and successful restore drill |
| NFR-B2 | Zero-downtime production delivery | Safe Compose deployment exists | `V2` | Versioned migration runner, readiness/draining and rolling/blue-green test |
| NFR-B3 | Reporting scale | Aging MV and journal pagination implemented | `V2` | Representative load test, query plan evidence and replica/reporting decision |

## F. Submission Completion

| ID | Submission requirement | Status | Remaining action |
|---|---|---|---|
| SUB1 | Repository, README, Compose, schema/migrations and sample requests | `COMPLETE` | Final commit/push only |
| SUB2 | Consolidated Markdown submission | `PARTIAL` | Final accuracy/length pass after decisions; ensure it is self-contained |
| SUB3 | AI tools and how they assisted | `COMPLETE` | Final wording check only |
| SUB4 | Approximate time per section | `USER INPUT` | Pankaj must provide truthful estimates |
| SUB5 | Four enterprise-experience examples | `USER INPUT` | Complete `docs/experience-showcase.md` from Pankaj's real experience |
| SUB6 | GAAP/IFRS assumptions | `COMPLETE` | Point-in-time, deferred revenue, tax, FX and period-close assumptions are explicit |

## Decision Queue

Work through only one item at a time:

1. `B7`--`B8` -- Bonus endpoint verification/hardening; `B9` period-management decision
2. `NFR2`--`NFR-B3` -- Production-hardening decisions
3. `SUB4`--`SUB5` -- Candidate/final-submission completion
