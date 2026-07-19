# Design Tradeoffs — ERP AR Module

Every significant design decision with options considered, tradeoffs weighed, and choice made.

---

## T1 — Architecture: Modular Monolith vs Microservices

**Q: Should we use microservices or a modular monolith?**

| | Modular Monolith | Microservices |
|--|--|--|
| Transactions | Single ACID transaction | Distributed saga pattern |
| Complexity | Low | High |
| Ops overhead | Low | High |
| Team size needed | Small (5 people) | Large |
| Latency | No network hops | Inter-service calls |

**Decision: Modular Monolith**
Invoice creation needs invoice + line_items in one ACID transaction. Payment needs allocation + GL entry + balance update atomically. A 5-person startup cannot operate microservices effectively. Monolith can be decomposed later when complexity justifies it.

---

## T2 — Consistency vs Availability (CAP theorem)

**Q: Should the system prioritize consistency or availability?**

| | Consistency (CP) | Availability (AP) |
|--|--|--|
| Wrong balance shown | Never | Possible |
| Double payment risk | None | Possible |
| Downtime behavior | 503 returned | Stale data returned |
| Financial audit | Clean | Risk of discrepancy |

**Decision: Consistency (CP)**
Financial systems must never show wrong balances. A 503 is acceptable. A wrong invoice amount is not. System returns 503 rather than serve stale financial data.

Exception: AR aging report — 5 min staleness acceptable. Collections team does not need millisecond accuracy.

---

## T3 — Database: PostgreSQL only vs PostgreSQL + Redis

**Q: Should we add Redis for caching and locking?**

| | PostgreSQL only | PostgreSQL + Redis |
|--|--|--|
| Consistency | ACID guaranteed | Redis not durable |
| Infrastructure | Simple | Extra component |
| Payment locking | DB serializable | Redis lock (lossy) |
| Cost | Low | Extra ops + cost |
| Risk | Low | Redis down = lock lost |

**Decision: PostgreSQL only**
Redis is in-memory — not durable. Redis down = lock lost = double payment risk. PostgreSQL serializable isolation handles concurrent payments safely. No Redis means simpler ops, fewer failure modes.

Exception: Redis acceptable for non-financial read cache in Phase 2.

---

## T4 — Payment Isolation Level: Serializable vs Repeatable Read

**Q: What isolation level for payment allocation?**

| | Read Committed | Repeatable Read | Serializable |
|--|--|--|--|
| Performance | Fastest | Medium | Slowest |
| Double allocation risk | High | Medium | None |
| Invoice approval | Overkill | Correct | Overkill |
| Payment allocation | Dangerous | Risky | Correct |

**Decision: Selective isolation levels**
- Invoice creation: Read Committed
- Invoice approval: Repeatable Read
- Payment allocation: Serializable
- Aging report: Read Committed

---

## T5 — Idempotency: Redis vs PostgreSQL

**Q: Where to store idempotency keys?**

| | Redis | PostgreSQL |
|--|--|--|
| Speed | ~1ms | ~5ms |
| Durability | In-memory, lossy | ACID, durable |
| Crash safety | Key lost on crash | Key survives crash |
| Same transaction | No | Yes |
| Financial safety | Risk | Safe |

**Decision: PostgreSQL**
Idempotency key must be committed in same ACID transaction as the payment. Redis + DB = two separate systems = possible inconsistency on crash.

---

## T6 — Concurrency Control: Optimistic vs Pessimistic Locking

**Q: How to prevent concurrent invoice modifications?**

| | Pessimistic (DB lock) | Optimistic (version column) | Redis lock |
|--|--|--|--|
| Durability | ACID | ACID | Lossy |
| Performance | Blocks reads | Non-blocking | Fast but unsafe |
| ABA problem | Prevented | Prevented | Not prevented |
| Deadlock risk | Yes | No | No |

**Decision: Optimistic locking with version column**
Version column increments on every invoice mutation. If-Match header carries version from client. Server rejects if version mismatch → 409 Conflict → client refreshes. Prevents ABA problem. No deadlocks. No external dependencies.

---

## T7 — JWT Validation: Gateway only vs Zero Trust

**Q: Should AR App re-validate JWT if gateway already did?**

| | Gateway only | Zero Trust (both) |
|--|--|--|
| Security | Single point of failure | Defense in depth |
| Bypass risk | App vulnerable if GW bypassed | App always safe |
| Performance | Slightly faster | ~2ms extra |
| Complexity | Simpler | Slightly more code |

**Decision: Zero Trust**
Someone can bypass gateway and call AR App directly with a forged JWT. AR App independently validates JWT using JWKS endpoint. ~2ms overhead negligible. Industry standard (Google BeyondCorp).

---

## T8 — AR Aging: Live Query vs Materialized View vs Redis Cache

**Q: How to serve GET /customers/{id}/aging efficiently?**

| | Live JOIN | Materialized View | Redis Cache |
|--|--|--|--|
| Freshness | Always fresh | 5 min stale | TTL stale |
| NFR compliance | Yes | Yes (5 min ok) | No (violates no-Redis) |
| Performance | Slow at scale | Fast | Fast |
| Complexity | Simple | Medium | High (invalidation) |

**Decision: Materialized View**
Collections team reviews aging once daily — 5 min staleness acceptable. Redis violates NFR1. Live JOIN at 10,000 concurrent users = DB overload. MV refreshed by pg_cron every 5 minutes. as_of timestamp shown in response.

---

## T9 — MV Refresh: On Write vs Cron vs pg_cron

**Q: When should the AR aging materialized view be refreshed?**

| | On every write | Background cron | pg_cron (in DB) |
|--|--|--|--|
| Freshness | Immediate | 5 min stale | 5 min stale |
| Write performance | Slower | Fast | Fast |
| Infra complexity | Low | Extra container | Zero extra |
| Duplication risk (distributed) | None | Yes | None |
| Production viable | Mid-scale | K8s CronJob | Mid-market |

**Decision: pg_cron**
pg_cron runs once inside PostgreSQL — no separate scheduler container and no duplication across AR App instances. Its metadata lives in the `postgres` system database and the job targets `erp_db`. `REFRESH MATERIALIZED VIEW CONCURRENTLY` keeps the previous complete snapshot readable while rebuilding; the unique `(tenant_id, entity_id, customer_id)` index makes that possible. Refresh every 5 minutes is acceptable for the collections team. Production at enterprise scale → K8s CronJob or AWS EventBridge.

---

## T10 — Invoice Detail Cache: Live JOIN vs MV vs Redis

**Q: Should GET /invoices/{id} use a cache?**

| | Live JOIN | Materialized View | Redis Cache |
|--|--|--|--|
| Freshness | Always fresh | Stale | TTL stale |
| NFR compliance | Yes | No (staleness not ok) | No (no Redis) |
| Performance | ~50ms (indexed) | ~1ms | ~1ms |
| Complexity | Simple | High | High |

**Decision: Live JOIN**
NFR2 requires < 100ms — live JOIN with indexes achieves this. Invoice detail staleness NOT acceptable (CFO approves based on what they see). Redis violates no-Redis decision.

---

## T11 — Invoice Sending: Outbox vs Direct HTTP vs SQS

**Q: How to deliver invoices to customers?**

| | Direct HTTP to stub | PostgreSQL outbox + worker | Direct SQS |
|--|--|--|--|
| Prototype complexity | Simple | Medium | Medium |
| Approval/event atomicity | No | Yes | No |
| Retry after process crash | Fragile | Durable | Durable after publish |
| Extra infra | None | Worker; existing DB | SQS |

**Decision: PostgreSQL transactional outbox + worker**
Approval, GL entries, idempotency result, and a PENDING delivery event commit in one transaction. A separate Docker worker uses `FOR UPDATE SKIP LOCKED`, calls the stub, and retries with exponential backoff; exhausted events become DEAD for operational intervention. Delivery is at-least-once, so the stable outbox event ID is sent as the downstream idempotency key. Production can retain the outbox and publish to SQS before Email/EDI/IRP adapters.

---

## T12 — API Gateway: L4 vs L7

**Q: Should we use L4 or L7 API Gateway?**

| | L4 (TCP level) | L7 (HTTP level) |
|--|--|--|
| JWT validation | Cannot read headers | Yes |
| Rate limiting by tenant | No | Yes |
| SSL termination | Yes | Yes |
| Routing by path | No | Yes |

**Decision: L7**
We need JWT validation at gateway layer — requires reading HTTP headers. L4 operates at TCP/IP level — cannot inspect HTTP headers.

---

## T13 — Cron in Distributed System: Naive vs Leader Election vs pg_cron

**Q: How to run scheduled MV refresh without duplication across AR App instances?**

| | Cron on each instance | Leader election | pg_cron |
|--|--|--|--|
| Duplication risk | High | None | None |
| Complexity | Low | High | Low |
| Extra infra | None | Redis/ZK | None |
| Failure handling | Multiple retries | Complex | PostgreSQL handles |

**Decision: pg_cron**
Naive cron on each AR App instance = 3 simultaneous refreshes = lock contention. Leader election needs Redis or ZooKeeper = complexity + no-Redis violation. pg_cron runs inside PostgreSQL = single scheduler = zero duplication = zero extra infra.

---

## T14 — Pagination: Cursor vs Page-based

**Q: What pagination strategy for GET /journal-entries?**

| | Page-based (OFFSET) | Cursor-based |
|--|--|--|
| Performance at page 1 | Fast | Fast |
| Performance at page 500 | Slow (OFFSET 9980) | Always fast |
| Complexity | Simple | Medium |
| Use case fit | Small result sets | Large result sets |

**Decision: Page-based for invoice-scoped queries**
One invoice has max 10-20 journal entries (create + payments + credit memos). OFFSET problem doesn't apply at this scale. Cursor-based pagination added in Phase 2 for date-range queries across all journal entries.

---
