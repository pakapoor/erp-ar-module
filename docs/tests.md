# Test Dashboard -- ERP AR Module

## Current Result

Verified on 22 July 2026 using the Docker Compose development environment.

| Layer | Tests/checks | Passed | Failed | Pass rate |
|---|---:|---:|---:|---:|
| Python unit tests | 84 | 84 | 0 | 100% |
| API and control assertions | 141 | 141 | 0 | 100% |
| **Total automated checks** | **225** | **225** | **0** | **100%** |

The two rows intentionally remain separate: unit tests are `unittest` test
cases, while integration results are explicit shell assertions against the
running system. Their combined count is a useful dashboard total, not a claim
that they use the same test runner.

## Coverage

| Metric | Result | Gate |
|---|---:|---:|
| Combined Python statement/branch coverage across `src` | **72.6%** | **70% minimum** |
| Unit-test cases executed | 84 | All must pass |

Coverage.py measures the complete `src` tree. No application module is omitted
to improve the percentage. `.coveragerc` sets `fail_under = 70`, so
`./tests/run_coverage.sh` exits unsuccessfully if coverage regresses below the
target.

Selected module results from the verified run:

| Module | Coverage |
|---|---:|
| `src/routers/aging.py` | 100.0% |
| `src/routers/delivery.py` | 98.1% |
| `src/routers/journal_entries.py` | 96.0% |
| `src/seed_data.py` | 93.5% |
| `src/schemas.py` | 93.9% |
| `src/main.py` | 89.1% |
| `src/delivery_worker.py` | 86.9% |
| `src/routers/health.py` | 81.4% |
| `src/fx_rate_worker.py` | 79.1% |
| `src/delivery_publisher.py` | 77.0% |
| `src/routers/invoices.py` | 56.8% |
| `src/routers/payments.py` | 41.7% |
| `src/routers/credit_memos.py` | 32.8% |

The lower command-router percentages remain visible in the report.
`invoices.py` rose from 24.9% to 56.8% after `test_invoice_patch_reject.py` was
added (PATCH and reject guard clauses and happy paths); `payments.py` and
`credit_memos.py` still lean on the integration suites for their end-to-end
financial behavior. Future unit work should deepen individual payment,
credit, void and write-off branches rather than narrowing the coverage scope.

## Test Structure

```text
tests/
+-- run_coverage.sh
+-- unit/
|   +-- test_auth.py
|   +-- test_endpoints.py
|   +-- test_financial_guards.py
|   +-- test_fx_rate_worker.py
|   +-- test_invoice_fx.py
|   +-- test_invoice_patch_reject.py
|   +-- test_router_helpers.py
|   +-- test_seed_and_database.py
|   +-- test_worker_persistence.py
|   +-- test_workers.py
+-- integration/
|   +-- test_api.sh
|   +-- test_api_negative.sh
|   +-- test_credit_memo.sh
|   +-- test_delivery_sqs.sh
+-- concurrency/
    +-- test_payment_concurrency.sh
```

### Unit tests

The unit suite covers JWT and RBAC failure modes, financial calculations,
schema validation, idempotency guards, report builders, API response logic,
database transaction behavior, seed operations, outbox publishing, SQS
consumption, FX-worker orchestration, and PATCH/reject invoice guard clauses
and happy paths. External systems and database sessions are mocked at their
boundaries.

### Integration and control tests

| Suite | Assertions | Main purpose |
|---|---:|---|
| `tests/integration/test_api.sh` | 76 | API1--API7, API3b (reject/patch/re-approve), FX, accounting, idempotency, isolation and reconciliation |
| `tests/integration/test_api_negative.sh` | 34 | Malformed JWT/header/body resilience and post-burst health |
| `tests/concurrency/test_payment_concurrency.sh` | 3 | AUTO/MANUAL payment races and reconciliation |
| `tests/integration/test_credit_memo.sh` | 17 | Entity, idempotency, concurrency, paid/partial, FX and GL controls |
| `tests/integration/test_delivery_sqs.sh` | 11 | Outbox, SQS, DLQ, deduplication, failure isolation and CFO retry |
| **Total** | **141** | **All assertions passed** |

## Run the Tests

Full deployment and integration verification:

```bash
./deploy.sh --test
```

Unit coverage and the enforced 70% gate:

```bash
./tests/run_coverage.sh
```

For individual commands, expected financial results and troubleshooting, see
[Verification and Expected Results](testing.md).

## Result Interpretation

- **100% pass rate** means every test/check executed in the verified run passed;
  it does not mean every possible input has been tested.
- **72.6% coverage** measures executed Python statements and branches in unit
  tests; it does not count shell-driven API execution as Python unit coverage.
- Financial correctness is also checked through balanced journals, AR-to-GL
  reconciliation, rollback behavior, idempotent replay and concurrency races.
