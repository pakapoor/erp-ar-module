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

Expected: four services are running:

| Service | Container | Expected state | Purpose |
|---|---|---|---|
| `app` | `erp_app` | Up | FastAPI AR application on port 8000 |
| `db` | `erp_db` | Up (healthy) | PostgreSQL with pg_cron on port 5432 |
| `stub` | `erp_stub` | Up | JWKS and delivery console stub on port 9000 |
| `delivery_worker` | `erp_delivery_worker` | Up | Transactional-outbox consumer |

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

## 2. Run the repeatable integration suite

```bash
./test_api.sh
```

The script safely reruns because seed inserts use `ON CONFLICT DO NOTHING` and
write requests use deterministic idempotency keys. A rerun returns cached
committed responses instead of creating duplicate invoices, payments, or GL
entries.

### Assertions and expected results

| Step | Expected result |
|---|---|
| Seed/JWT setup | Reliance tenant/entity/users/customer/GL accounts/OPEN July 2026 period exist; fresh one-hour development JWTs are generated |
| API1 `POST /invoices` | HTTP 201; DRAFT version 1; server-calculated subtotal INR 150,000, tax INR 24,000, total INR 174,000 |
| API2 `GET /invoices/{id}` | HTTP 200; same invoice and two line items; ETag reflects the current version |
| API3 `POST /invoices/{id}/approve` | HTTP 200; APPROVED version 2; approval journal ID returned; delivery status QUEUED on a fresh run |
| Delivery outbox | Approval event reaches DELIVERED within 10 seconds; stub receives the stable delivery-event ID |
| API4 `POST /payments` | HTTP 201; INR 100,000 allocated; invoice becomes PARTIALLY_PAID with INR 74,000 balance |
| API4 same-key retry | Original payment ID and response are returned; no second payment is created |
| API5 customer aging | HTTP 200; current bucket contains one invoice totaling INR 74,000 |
| API6 invoice journals | HTTP 200; two entries; every entry balances; net GL AR is INR 74,000 |
| API7 health | HTTP 200; database healthy and AR/GL reconciliation MATCHED |
| Cross-tenant read | HTTP 404 so record existence is concealed |
| Creator approval attempt | HTTP 403 due to RBAC/SOX separation |
| Same idempotency key, changed payment | HTTP 409; changed request is not replayed |

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

## 5. Verify pg_cron aging refresh

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
- schedule `*/5 * * * *`
- target database `erp_db`
- command `REFRESH MATERIALIZED VIEW CONCURRENTLY ar_aging`

Execution history:

```bash
docker compose exec -T db psql -U erp_user -d postgres -P pager=off -c "
SELECT status, start_time, end_time, return_message
FROM cron.job_run_details
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

  Apply each migration only after checking which versions that development
  database already contains; migration `004` is not designed to recreate an
  existing policy repeatedly.
- Port conflict: override `BASE_URL` for the script only if the app is exposed
  elsewhere, for example `BASE_URL=http://localhost:8080 ./test_api.sh`.
