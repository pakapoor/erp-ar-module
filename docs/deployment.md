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

Ports 5432, 8000, and 9000 must be available. Port 8000 belongs to Envoy; the
FastAPI application listens on port 8080 only inside the Compose network.

## Deploy

```bash
chmod +x deploy.sh
./deploy.sh
```

Expected final output begins with `Deployment successful` and lists six
running containers:

- `erp_db` — healthy
- `erp_gateway` — Envoy, publicly exposed on port 8000
- `erp_app` — FastAPI, internal port 8080 with no host mapping
- `erp_stub` — running on port 9000
- `erp_delivery_worker` — running
- `erp_fx_rate_worker` — running; imports official ECB reference rates

The script prints these endpoints:

```text
L7 Gateway:    http://localhost:8000
API docs:      http://localhost:8000/docs
Delivery stub: http://localhost:9000
Live FX feed:  docker compose logs -f fx_rate_worker
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
2. Build the images and validate the Envoy configuration before changing the
   running stack.
3. Start the containerized PostgreSQL plus delivery/JWKS stub.
4. Wait for PostgreSQL readiness.
5. Apply migration 002 only if aging-count columns are missing.
6. Idempotently enable pg_cron and ensure one aging refresh plus one weekday FX
   import-enqueue schedule.
7. Apply migration 004 only if `delivery_outbox` is missing.
8. Apply migration 005 when idempotency keys are not yet entity-scoped.
9. Apply migration 006 when `fx_import_job` and FX provenance/snapshot fields
   are missing.
10. Concurrently refresh the aging MV so deployment health is deterministic.
11. Start the internal AR application, outbox/FX workers, and Envoy gateway.
12. Require health through Envoy and from the stub, verify both workers, and prove
    that FastAPI port 8080 is not published to the host.
13. Optionally seed data and/or run `test_api.sh`.

If a command fails, the script exits nonzero and prints container state plus the
last 60 log lines from all services.

## Verified deployment results

The following paths were executed locally against the Docker Compose stack:

```bash
./deploy.sh --no-build --test
./deploy.sh --no-build --no-install --test
./deploy.sh --no-install --test
./deploy.sh
```

All exited with status 0. The test paths completed all API, outbox, accounting,
entity isolation, overpayment, optimistic-concurrency, health, idempotency,
gateway JWT, network-isolation, and trace-propagation assertions. The full
build correctly reported migrations 002, 004, 005, and 006 as
already present, reused both pg_cron jobs, refreshed the MV concurrently,
and returned all six services in a running/healthy state. Repeated executions
did not duplicate walkthrough financial records or the cron schedule; each
concurrency race uses a fresh invoice and settles it before exit.

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
