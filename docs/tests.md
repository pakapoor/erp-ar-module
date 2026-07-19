# Test Dashboard — ERP AR Module

## Current Result

Verified on 20 July 2026 using the Docker Compose development environment.

| Layer | Tests/checks | Passed | Failed | Pass rate |
|---|---:|---:|---:|---:|
| Python unit tests | 73 | 73 | 0 | 100% |
| API and control assertions | 127 | 127 | 0 | 100% |
| **Total automated checks** | **200** | **200** | **0** | **100%** |

The two rows intentionally remain separate: unit tests are `unittest` test
cases, while integration results are explicit shell assertions against the
running system. Their combined count is a useful dashboard total, not a claim
that they use the same test runner.

## Coverage

| Metric | Result | Gate |
|---|---:|---:|
| Combined Python statement/branch coverage across `src` | **70.9%** | **70% minimum** |
| Unit-test cases executed | 73 | All must pass |

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
| `src/main.py` | 89.1% |
| `src/delivery_worker.py` | 86.9% |
| `src/routers/health.py` | 81.4% |
| `src/fx_rate_worker.py` | 79.1% |
| `src/delivery_publisher.py` | 77.0% |

The lower command-router percentages remain visible in the report. Their
end-to-end financial behavior is exercised by the integration suites; future
unit work should deepen individual invoice, payment, credit, void and write-off
branches rather than narrowing the coverage scope.

## Test Structure

```text
tests/
├── run_coverage.sh
├── unit/
│   ├── test_auth.py
│   ├── test_endpoints.py
│   ├── test_financial_guards.py
│   ├── test_fx_rate_worker.py
│   ├── test_invoice_fx.py
│   ├── test_router_helpers.py
│   ├── test_seed_and_database.py
│   ├── test_worker_persistence.py
│   └── test_workers.py
├── integration/
│   ├── test_api.sh
│   ├── test_api_negative.sh
│   ├── test_credit_memo.sh
│   └── test_delivery_sqs.sh
└── concurrency/
    └── test_payment_concurrency.sh
```

### Unit tests

The unit suite covers JWT and RBAC failure modes, financial calculations,
schema validation, idempotency guards, report builders, API response logic,
database transaction behavior, seed operations, outbox publishing, SQS
consumption and FX-worker orchestration. External systems and database sessions
are mocked at their boundaries.

### Integration and control tests

| Suite | Assertions | Main purpose |
|---|---:|---|
| `tests/integration/test_api.sh` | 62 | API1–API7, FX, accounting, idempotency, isolation and reconciliation |
| `tests/integration/test_api_negative.sh` | 34 | Malformed JWT/header/body resilience and post-burst health |
| `tests/concurrency/test_payment_concurrency.sh` | 3 | AUTO/MANUAL payment races and reconciliation |
| `tests/integration/test_credit_memo.sh` | 17 | Entity, idempotency, concurrency, paid/partial, FX and GL controls |
| `tests/integration/test_delivery_sqs.sh` | 11 | Outbox, SQS, DLQ, deduplication, failure isolation and CFO retry |
| **Total** | **127** | **All assertions passed** |

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
- **70.9% coverage** measures executed Python statements and branches in unit
  tests; it does not count shell-driven API execution as Python unit coverage.
- Financial correctness is also checked through balanced journals, AR-to-GL
  reconciliation, rollback behavior, idempotent replay and concurrency races.
