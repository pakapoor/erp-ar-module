# Verification and Expected Results

This guide verifies the required assessment APIs, financial controls, pg_cron
aging refresh, and asynchronous invoice delivery. Run commands from the project
root.

## 1. Start the system

```bash
./deploy.sh
docker compose ps
```

The equivalent manual start is `docker compose up -d --build`, but the
deployment script also performs migration detection and health verification.

Expected: six services are running:

| Service | Container | Expected state | Purpose |
|---|---|---|---|
| `gateway` | `erp_gateway` | Up | Envoy L7 gateway on host port 8000 |
| `app` | `erp_app` | Up | FastAPI AR application on internal port 8080 only |
| `db` | `erp_db` | Up (healthy) | PostgreSQL with pg_cron on port 5432 |
| `stub` | `erp_stub` | Up | JWKS and delivery console stub on port 9000 |
| `delivery_worker` | `erp_delivery_worker` | Up | Transactional-outbox consumer |
| `fx_rate_worker` | `erp_fx_rate_worker` | Up | ECB reference-rate importer |

Quick health checks:

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
curl -s http://localhost:9000/health | python3 -m json.tool
```

Expected: the stub reports `healthy`. The AR endpoint returns HTTP 200 with
`database=healthy`, `materialized_view_status=healthy`, and
`reconciliation_status=MATCHED`. Immediately after first startup, the aging MV
may report stale until its first scheduled refresh; the integration test below
refreshes it explicitly before asserting health.

Every AR API command below enters through Envoy on port 8000. The FastAPI
container deliberately has no host-published port.

## 2. Run the repeatable integration suite

```bash
./test_api.sh
./test_payment_concurrency.sh
```

The script safely reruns because seed inserts use `ON CONFLICT DO NOTHING`, the
main walkthrough uses deterministic idempotency keys, and dynamically created
concurrency invoices are settled to zero before exit. A rerun returns cached
walkthrough responses without creating duplicate invoices, payments, or GL
entries; it creates a fresh, settled invoice for each real concurrency race.
`deploy.sh --test` runs both scripts.

Scope boundary: the automated suite currently verifies the required APIs, FX,
gateway/outbox controls and payment/approval races. The newly implemented bonus
credit-memo, write-off and void routes still require a dedicated acceptance
matrix before the traceability document can mark them complete.

### Assertions and expected results

| Step | Expected result |
|---|---|
| Seed/JWT setup | Reliance tenant/entity/users/customer/GL accounts/OPEN July 2026 period exist; fresh one-hour development JWTs are generated |
| Gateway missing JWT | HTTP 401 before the protected request reaches FastAPI |
| Gateway invalid signature | HTTP 401 for a correctly shaped token signed with the wrong secret |
| Application network isolation | Docker reports no host mapping for FastAPI port 8080 |
| Zero Trust application check | A direct Compose-network request with an invalid JWT is independently rejected with HTTP 401 by FastAPI |
| Trace propagation | Envoy-generated request ID is returned as `X-Trace-ID` by FastAPI |
| API1 `POST /invoices` | HTTP 201; DRAFT version 1; server-calculated subtotal INR 150,000, tax INR 24,000, total INR 174,000 |
| API1 foreign-currency invoice | Lowercase `usd` is normalized; an approved USD/INR rate ID and all INR base snapshots are stored; base components balance |
| API1 stale FX rate | HTTP 503; no invoice is created and the transaction rolls back |
| API3 foreign-currency approval | USD invoice becomes APPROVED version 2; one journal balances independently in USD transaction amounts and INR base amounts |
| API4 foreign-currency gain | Full USD receipt at a higher payment-date rate clears AR and credits the exact realized INR FX gain |
| API4 foreign-currency loss/partial | Two lower-rate receipts move APPROVED → PARTIALLY_PAID → PAID, debit both realized INR losses and leave zero AR |
| API4 FX controls | Cross-currency allocation returns 422; stale payment rate returns 503 and rolls back |
| API2 `GET /invoices/{id}` | HTTP 200; same invoice and two line items; ETag reflects the current version |
| API3 `POST /invoices/{id}/approve` | HTTP 200; APPROVED version 2; approval journal ID returned; delivery status QUEUED on a fresh run |
| Delivery outbox | Approval event reaches DELIVERED within 10 seconds; stub receives the stable delivery-event ID |
| API4 `POST /payments` | HTTP 201; INR 100,000 allocated; invoice becomes PARTIALLY_PAID with INR 74,000 balance |
| API4 same-key retry | Original payment ID and response are returned; no second payment is created |
| API5 customer aging | HTTP 200; current bucket contains one posted invoice totaling INR 74,000; foreign DRAFT invoices are excluded |
| API6 invoice journals | HTTP 200; transaction/base amounts and currencies are explicit; every entry balances in both representations; base net GL AR is INR 74,000 |
| API6 pagination pages 1 and 2 | One distinct journal per page; `total=2`, `total_pages=2`; both retain invoice-wide net AR INR 74,000 |
| API6 pagination page 3 | HTTP 200 with an empty journal list and unchanged invoice-wide summary |
| API6 invalid pagination | HTTP 422 for page 0 and page size 101 |
| API7 health | HTTP 200; every entity's posted base-currency AR subledger matches its base-currency AR GL |
| Cross-tenant read | HTTP 404 so record existence is concealed |
| Cross-entity invoice/journal/aging reads | HTTP 404 so sibling-entity records are concealed |
| Cross-entity idempotency-key collision | HTTP 404; another entity's cached response is never returned |
| Creator approval attempt | HTTP 403 due to RBAC/SOX separation |
| Same idempotency key, changed payment | HTTP 409; changed request is not replayed |
| FR4 overpayment | INR 1,500 cash receipt clears INR 1,000 AR and credits INR 500 to GL 2100 Customer Credit; journal balances |
| T6 stale version | HTTP 409 for stale `If-Match` |
| T6 simultaneous approval | Exactly one HTTP 200 and one HTTP 409; exactly one approval GL entry and one outbox event |
| T6 cleanup | Control invoice is paid to zero so repeated runs preserve deterministic aging |
| NFR1 MANUAL payment race | Exactly one HTTP 201 and one retryable 409; one allocation/journal; SENT v3 advances once to PAID v4 |
| NFR1 AUTO payment race | Same guarantee under FIFO allocation; isolated customer prevents unrelated invoices entering the race |
| NFR1 reconciliation | Both freshly created control invoices finish at zero balance and health remains HTTP 200/MATCHED |

The final line must be:

```text
=== All integration assertions passed ===
```

## 3. Inspect the final financial state

```bash
docker compose exec -T db psql -U erp_user -d erp_db -P pager=off -c "
SELECT id, status, version, total_amount, balance_amount, sent_at
FROM invoice
WHERE customer_id = '00000000-0000-0000-0000-000000000005';
"
```

Expected for the deterministic Tata Steel invoice:

- status `PARTIALLY_PAID`
- total `174000.0000`
- balance `74000.0000`
- `sent_at` populated
- version 4 on a clean run: create 1, approval 2, delivery 3, payment 4

Verify the outbox:

```bash
docker compose exec -T db psql -U erp_user -d erp_db -P pager=off -c "
SELECT invoice_id, event_type, status, attempt_count, delivered_at, last_error
FROM delivery_outbox
ORDER BY created_at;
"
```

Expected: `INVOICE_APPROVED`, status `DELIVERED`, at least one attempt,
`delivered_at` populated, and no `last_error`.

## 4. Watch asynchronous delivery

Open a second terminal before running the suite:

```bash
docker compose logs -f stub delivery_worker
```

Expected worker output:

```text
HTTP Request: POST http://stub:9000/stub/send-invoice "HTTP/1.1 200 OK"
Delivered outbox event <delivery-id> for invoice <invoice-id>
```

Expected stub output includes the same delivery ID and invoice ID, the customer,
amount, currency, and due date. Matching IDs demonstrate the stable downstream
idempotency key.

## 5. Verify pg_cron schedules

The scheduler metadata deliberately lives in the `postgres` system database;
the job targets the application database `erp_db`.

```bash
docker compose exec -T db psql -U erp_user -d postgres -P pager=off -c "
SHOW shared_preload_libraries;
SELECT jobname, schedule, database, command, active FROM cron.job;
"
```

Expected:

- `shared_preload_libraries` contains `pg_cron`
- one active job named `refresh-ar-aging-every-5-minutes`
- one active job named `enqueue-ecb-fx-import-weekdays`
- schedules `*/5 * * * *` and `30 15 * * 1-5` respectively
- both target database `erp_db`
- the first command refreshes `ar_aging` concurrently; the second idempotently
  inserts one daily `fx_import_job`

The FX worker claims the import, validates one coherent ECB batch, derives
foreign→INR pairs, and stores tenant-approved immutable rows. The walkthrough
then proves the complete B1 V1 accounting path: invoice/approval snapshots,
payment-date rates, realized gains and losses, partial settlement, stale-rate
rollback and mixed-currency rejection.

Deterministic feed tests:

```bash
docker compose exec -T app \
  python -m unittest -q src.tests.test_fx_rate_worker src.tests.test_invoice_fx
```

Expected: seven tests pass, covering feed validation/derivation, financial
rounding, lowercase normalization and unsupported-currency rejection.

Inspect the optional live import (internet availability is deliberately not a
gate for the deterministic suite):

```bash
docker compose logs --tail=30 fx_rate_worker
docker compose exec -T db psql -U erp_user -d erp_db -P pager=off -c "
SELECT provider, requested_date, provider_effective_date, status, attempt_count
FROM fx_import_job ORDER BY requested_date DESC LIMIT 3;
SELECT from_currency, to_currency, rate, effective_date, source, status
FROM exchange_rate WHERE source = 'ECB'
ORDER BY effective_date DESC, from_currency;
"
```

On a successful import, the latest job is `COMPLETED` and the configured tenant
has seven approved foreign→INR rows. A weekend request legitimately uses the
preceding Friday within the three-day policy.

Execution history:

```bash
docker compose exec -T db psql -U erp_user -d postgres -P pager=off -c "
SELECT status, start_time, end_time, return_message
FROM cron.job_run_details
WHERE jobid = (
  SELECT jobid FROM cron.job
  WHERE jobname = 'refresh-ar-aging-every-5-minutes'
)
ORDER BY runid DESC
LIMIT 5;
"
```

After a five-minute boundary, the latest result should be `succeeded` with
`REFRESH MATERIALIZED VIEW`. API5 readers retain the previous complete snapshot
while the concurrent refresh builds the next one.

## 6. Optional delivery retry drill

This drill intentionally requeues the newest **local test** delivery, so the
stub may log it twice. That is expected for at-least-once delivery. Do not run
this against production data.

```bash
docker compose stop stub

docker compose exec -T db psql -U erp_user -d erp_db -c "
UPDATE delivery_outbox
SET status = 'PENDING',
    delivered_at = NULL,
    next_attempt_at = NOW(),
    locked_at = NULL,
    last_error = NULL
WHERE id = (
  SELECT id FROM delivery_outbox ORDER BY created_at DESC LIMIT 1
);
"

sleep 5
docker compose logs --tail=20 delivery_worker
docker compose start stub
sleep 10

docker compose exec -T db psql -U erp_user -d erp_db -P pager=off -c "
SELECT id, status, attempt_count, delivered_at, last_error
FROM delivery_outbox
ORDER BY created_at DESC
LIMIT 1;
"
```

Expected sequence:

1. While the stub is stopped, the worker logs a failed attempt and persists the
   error with a future `next_attempt_at`.
2. After restart, the worker retries the same event ID.
3. Final status returns to `DELIVERED`, `attempt_count` has increased, and
   `last_error` is cleared.
4. The invoice and GL transaction remain committed throughout the outage.

## 7. Troubleshooting

```bash
docker compose logs --tail=100 app db stub delivery_worker
```

- `503` from `/health` with `materialized_view_status=stale`: wait for pg_cron
  or refresh explicitly with
  `REFRESH MATERIALIZED VIEW CONCURRENTLY ar_aging`.
- Outbox remains PENDING: confirm both `delivery_worker` and `stub` are Up, then
  inspect `last_error` and `next_attempt_at`.
- Migration/table missing on an old volume: Docker entrypoint migrations only
  run when a volume is first initialized. Preserve the volume and run:

  ```bash
  docker compose up -d --build --force-recreate db
  docker compose exec -T db /docker-entrypoint-initdb.d/003_setup_pg_cron.sh
  docker compose exec -T db psql -U erp_user -d erp_db \
    -f /docker-entrypoint-initdb.d/004_delivery_outbox.sql
  ```

  `deploy.sh` detects and applies migration 005 automatically when idempotency
  keys are not yet entity-scoped. Apply each migration only after checking
  which versions that development database already contains; migration `004`
  is not designed to recreate an existing policy repeatedly.
- Port conflict: override `BASE_URL` for the script only if the app is exposed
  elsewhere, for example `BASE_URL=http://localhost:8080 ./test_api.sh`.
