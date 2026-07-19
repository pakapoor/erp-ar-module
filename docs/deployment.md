# Local Deployment Guide

`deploy.sh` is the deployment entry point for the Docker Compose prototype. It
is intentionally safe for a developer workstation: it never deletes the named
PostgreSQL volume and does not reset financial data.

## Prerequisites

- Docker Desktop with Docker Compose v2
- `curl`
- ports 5432, 8000, and 9000 available

## Deploy

```bash
chmod +x deploy.sh
./deploy.sh
```

Expected final output begins with `Deployment successful` and lists four
running containers:

- `erp_db` — healthy
- `erp_app` — running on port 8000
- `erp_stub` — running on port 9000
- `erp_delivery_worker` — running

The script prints these endpoints:

```text
AR API:        http://localhost:8000
API docs:      http://localhost:8000/docs
Delivery stub: http://localhost:9000
```

## Options

| Command | Use |
|---|---|
| `./deploy.sh` | Build images, migrate, deploy, and verify health |
| `./deploy.sh --no-build` | Reuse existing images for a faster restart |
| `./deploy.sh --seed` | Deploy and insert deterministic interview/demo data |
| `./deploy.sh --test` | Deploy and run the complete API/control integration suite |
| `./deploy.sh --seed --test` | Explicit seed plus full verification; the test also seeds safely |

Run `./deploy.sh --help` for the same option summary.

## Deployment sequence

1. Verify Docker, Compose configuration, and required commands.
2. Build and start PostgreSQL plus the delivery/JWKS stub.
3. Wait for PostgreSQL readiness.
4. Apply migration 002 only if aging-count columns are missing.
5. Idempotently enable pg_cron and ensure exactly one five-minute aging job.
6. Apply migration 004 only if `delivery_outbox` is missing.
7. Concurrently refresh the aging MV so deployment health is deterministic.
8. Start the AR application and outbox worker.
9. Require HTTP health from the AR application and stub, plus a running worker.
10. Optionally seed data and/or run `test_api.sh`.

If a command fails, the script exits nonzero and prints container state plus the
last 60 log lines from all services.

## Expected health result

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
```

Expected:

```json
{
  "status": "healthy",
  "checks": {
    "database": "healthy",
    "materialized_view_status": "healthy",
    "reconciliation_status": "MATCHED"
  }
}
```

Additional response fields such as timestamps and MV age vary per run.

## Data and migration safety

- No `docker compose down -v`, volume removal, table truncation, or database
  reset is performed.
- Seed inserts use `ON CONFLICT DO NOTHING`.
- pg_cron setup is rerunnable and does not create duplicate jobs.
- Migration 004 is executed only when the outbox table is absent because its
  policy creation is intentionally one-time DDL.
- The aging MV uses a concurrent refresh, so readers retain the previous
  complete snapshot during deployment.

This object-detection approach is appropriate for the time-boxed prototype. A
production deployment should use versioned Alembic/Flyway migrations executed
once by the delivery pipeline, immutable images without `--reload`, managed
secrets, a non-owner database runtime role, readiness probes, and a controlled
rolling or blue/green rollout.

## Verification after deployment

For exact API steps, SQL inspection commands, pg_cron proof, expected financial
values, live outbox logs, and the optional retry drill, see
[Verification and Expected Results](testing.md).

