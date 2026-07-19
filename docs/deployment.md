# Local Deployment Guide

`deploy.sh` is the deployment entry point for the Docker Compose prototype. It
is intentionally safe for a developer workstation: it never deletes the named
PostgreSQL volume and does not reset financial data.

## Automatic prerequisites

`deploy.sh` checks its host dependencies before deployment. When one is
missing, it installs it using Homebrew on macOS or `apt-get`/`dnf`/`yum` on
Linux:

- Docker (Docker Desktop on macOS, Docker Engine on Linux)
- Docker Compose v2
- `curl`
- Python 3 when `--test` is selected

On macOS, Homebrew must already be installed because silently bootstrapping a
system package manager is outside this project's deployment boundary. On Linux,
installation or daemon startup may request `sudo` access. Use `--no-install` to
disable automatic installation and fail on a missing dependency instead.

PostgreSQL is deliberately **not** installed on the host. The custom
`erp-ar-postgres:16-pgcron` image contains PostgreSQL and pg_cron; Compose starts
it as the `erp_db` service. A second host installation could conflict on port
5432 and would not contain the project's initialized schema.

Ports 5432, 8000, and 9000 must be available.

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
| `./deploy.sh --no-install` | Do not install missing host dependencies |

Run `./deploy.sh --help` for the same option summary.

## Deployment sequence

1. Install missing host dependencies, start Docker, and verify Compose.
2. Build and start the containerized PostgreSQL plus delivery/JWKS stub.
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

## Verified deployment results

The following paths were executed locally against the Docker Compose stack:

```bash
./deploy.sh --no-build --test
./deploy.sh
```

Both exited with status 0. The first completed all API, outbox, accounting,
security, health, and idempotency assertions. The second rebuilt the images,
correctly reported migrations 002 and 004 as already present, reused the single
pg_cron job, refreshed the MV concurrently, and returned all four services in a
running/healthy state. This second execution is also the rerun-safety proof: it
did not recreate financial records or duplicate the cron schedule.

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
