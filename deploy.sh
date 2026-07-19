#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

BUILD_IMAGES=true
RUN_SEED=false
RUN_TESTS=false

usage() {
  cat <<'EOF'
Usage: ./deploy.sh [options]

Deploy the local Docker Compose prototype without deleting its database volume.

Options:
  --no-build   Reuse existing Docker images
  --seed       Insert deterministic demonstration data
  --test       Run the complete integration suite after deployment
  -h, --help   Show this help
EOF
}

for argument in "$@"; do
  case "$argument" in
    --no-build)
      BUILD_IMAGES=false
      ;;
    --seed)
      RUN_SEED=true
      ;;
    --test)
      RUN_TESTS=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $argument" >&2
      usage >&2
      exit 2
      ;;
  esac
done

log() {
  echo
  echo "==> $1"
}

show_failure_context() {
  local exit_code=$?
  set +e
  echo >&2
  echo "Deployment failed. Current service state:" >&2
  docker compose ps >&2
  echo >&2
  echo "Recent service logs:" >&2
  docker compose logs --tail=60 db app stub delivery_worker >&2
  exit "$exit_code"
}
trap show_failure_context ERR

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

wait_for_database() {
  local attempt
  for ((attempt = 1; attempt <= 30; attempt++)); do
    if docker compose exec -T db pg_isready -U erp_user -d erp_db >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "PostgreSQL did not become ready within 60 seconds" >&2
  return 1
}

wait_for_url() {
  local url=$1
  local service_name=$2
  local attempt
  for ((attempt = 1; attempt <= 30; attempt++)); do
    if curl --fail --silent --output /dev/null "$url"; then
      return 0
    fi
    sleep 2
  done
  echo "$service_name did not become healthy within 60 seconds: $url" >&2
  return 1
}

require_command docker
require_command curl
if [ "$RUN_TESTS" = true ]; then
  require_command python3
fi

log "Checking Docker and Compose"
docker info >/dev/null
docker compose version
docker compose config --quiet

if [ "$BUILD_IMAGES" = true ]; then
  log "Building deployment images"
  docker compose build
fi
up_options=(-d)

log "Starting PostgreSQL and the delivery/JWKS stub"
docker compose up "${up_options[@]}" db stub
wait_for_database

log "Applying missing database migrations"
has_aging_counts="$(
  docker compose exec -T db psql -U erp_user -d erp_db -Atc \
    "SELECT EXISTS (
       SELECT 1
       FROM pg_attribute attribute
       JOIN pg_class relation ON relation.oid = attribute.attrelid
       JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
       WHERE namespace.nspname = 'public'
         AND relation.relname = 'ar_aging'
         AND relation.relkind = 'm'
         AND attribute.attname = 'current_count'
         AND attribute.attnum > 0
         AND NOT attribute.attisdropped
     );"
)"
if [ "$has_aging_counts" != "t" ]; then
  docker compose exec -T db psql -U erp_user -d erp_db \
    -v ON_ERROR_STOP=1 \
    -f /docker-entrypoint-initdb.d/002_add_ar_aging_bucket_counts.sql
else
  echo "Migration 002 already present"
fi

# Safe to run repeatedly: creates the extension and job only when absent.
docker compose exec -T db /docker-entrypoint-initdb.d/003_setup_pg_cron.sh

outbox_table="$(
  docker compose exec -T db psql -U erp_user -d erp_db -Atc \
    "SELECT COALESCE(to_regclass('public.delivery_outbox')::text, '');"
)"
if [ "$outbox_table" != "delivery_outbox" ]; then
  docker compose exec -T db psql -U erp_user -d erp_db \
    -v ON_ERROR_STOP=1 \
    -f /docker-entrypoint-initdb.d/004_delivery_outbox.sql
else
  echo "Migration 004 already present"
fi

log "Refreshing the aging snapshot for deterministic health verification"
docker compose exec -T db psql -U erp_user -d erp_db \
  -v ON_ERROR_STOP=1 \
  -c "REFRESH MATERIALIZED VIEW CONCURRENTLY ar_aging" >/dev/null

log "Starting the AR application and delivery worker"
docker compose up "${up_options[@]}" app delivery_worker

log "Waiting for service health checks"
wait_for_url "http://localhost:9000/health" "Delivery stub"
wait_for_url "http://localhost:8000/health" "AR application"

worker_container="$(docker compose ps --status running --quiet delivery_worker)"
if [ -z "$worker_container" ]; then
  echo "Delivery worker is not running" >&2
  exit 1
fi

if [ "$RUN_SEED" = true ]; then
  log "Seeding deterministic demonstration data"
  docker compose exec -T app python -m src.seed_data
fi

if [ "$RUN_TESTS" = true ]; then
  log "Running integration tests"
  ./test_api.sh
fi

log "Deployment successful"
docker compose ps
echo
echo "AR API:        http://localhost:8000"
echo "API docs:      http://localhost:8000/docs"
echo "Delivery stub: http://localhost:9000"
echo "Live delivery: docker compose logs -f stub delivery_worker"
