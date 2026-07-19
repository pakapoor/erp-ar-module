#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

BUILD_IMAGES=true
RUN_SEED=false
RUN_TESTS=false
AUTO_INSTALL=true

usage() {
  cat <<'EOF'
Usage: ./deploy.sh [options]

Deploy the local Docker Compose prototype without deleting its database volume.

Options:
  --no-build    Reuse existing Docker images
  --seed        Insert deterministic demonstration data
  --test        Run the complete integration suite after deployment
  --no-install  Fail instead of installing missing host dependencies
  -h, --help    Show this help
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
    --no-install)
      AUTO_INSTALL=false
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

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

run_as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command_exists sudo; then
    sudo "$@"
  else
    echo "Root access is required to install or start Docker: $*" >&2
    return 1
  fi
}

install_macos_dependency() {
  local dependency=$1

  if ! command_exists brew; then
    echo "Homebrew is required for automatic installation on macOS." >&2
    echo "Install it from https://brew.sh, then rerun ./deploy.sh." >&2
    return 1
  fi

  case "$dependency" in
    docker)
      log "Installing Docker Desktop"
      brew install --cask docker
      ;;
    curl|python3)
      log "Installing $dependency"
      brew install "${dependency/python3/python}"
      ;;
  esac
}

install_linux_dependencies() {
  local packages=("$@")

  if command_exists apt-get; then
    log "Installing missing host dependencies: ${packages[*]}"
    run_as_root apt-get update
    run_as_root apt-get install -y "${packages[@]}"
  elif command_exists dnf; then
    log "Installing missing host dependencies: ${packages[*]}"
    run_as_root dnf install -y "${packages[@]}"
  elif command_exists yum; then
    log "Installing missing host dependencies: ${packages[*]}"
    run_as_root yum install -y "${packages[@]}"
  else
    echo "No supported Linux package manager found (apt-get, dnf, or yum)." >&2
    return 1
  fi
}

ensure_command() {
  local command_name=$1

  if command_exists "$command_name"; then
    return 0
  fi
  if [ "$AUTO_INSTALL" != true ]; then
    echo "Required command not found: $command_name" >&2
    return 1
  fi

  case "$(uname -s)" in
    Darwin)
      install_macos_dependency "$command_name"
      ;;
    Linux)
      case "$command_name" in
        docker)
          if command_exists apt-get; then
            install_linux_dependencies docker.io
          else
            install_linux_dependencies docker
          fi
          ;;
        python3) install_linux_dependencies python3 ;;
        curl) install_linux_dependencies curl ;;
        *)
          echo "No automatic installer configured for: $command_name" >&2
          return 1
          ;;
      esac
      ;;
    *)
      echo "Automatic dependency installation is unsupported on $(uname -s)." >&2
      return 1
      ;;
  esac

  if ! command_exists "$command_name"; then
    echo "Installation completed but '$command_name' is still unavailable." >&2
    return 1
  fi
}

start_docker_daemon() {
  if docker info >/dev/null 2>&1; then
    return 0
  fi

  log "Starting Docker"
  case "$(uname -s)" in
    Darwin)
      open -a Docker
      ;;
    Linux)
      if command_exists systemctl; then
        run_as_root systemctl start docker
      elif command_exists service; then
        run_as_root service docker start
      else
        echo "Cannot start Docker: systemctl/service is unavailable." >&2
        return 1
      fi
      ;;
  esac

  local attempt
  for ((attempt = 1; attempt <= 90; attempt++)); do
    if docker info >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "Docker did not become ready within 180 seconds." >&2
  return 1
}

ensure_compose() {
  if docker compose version >/dev/null 2>&1; then
    return 0
  fi
  if [ "$AUTO_INSTALL" != true ]; then
    echo "Docker Compose v2 is required." >&2
    return 1
  fi

  case "$(uname -s)" in
    Darwin)
      echo "Docker Desktop was installed, but Compose v2 is unavailable." >&2
      echo "Open Docker Desktop once, finish its setup, and rerun this script." >&2
      return 1
      ;;
    Linux)
      if command_exists apt-get; then
        install_linux_dependencies docker-compose-v2
      elif command_exists dnf; then
        install_linux_dependencies docker-compose-plugin
      elif command_exists yum; then
        install_linux_dependencies docker-compose-plugin
      fi
      ;;
  esac

  docker compose version >/dev/null 2>&1 || {
    echo "Docker Compose v2 could not be installed automatically." >&2
    return 1
  }
}

show_failure_context() {
  local exit_code=$?
  set +e
  echo >&2
  echo "Deployment failed. Current service state:" >&2
  if command_exists docker && docker compose version >/dev/null 2>&1; then
    docker compose ps >&2
    echo >&2
    echo "Recent service logs:" >&2
    docker compose logs --tail=60 gateway db app stub delivery_worker fx_rate_worker >&2
  else
    echo "Docker Compose is not available; service diagnostics were skipped." >&2
  fi
  exit "$exit_code"
}
trap show_failure_context ERR

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

ensure_command docker
ensure_command curl
if [ "$RUN_TESTS" = true ]; then
  ensure_command python3
fi

start_docker_daemon
ensure_compose

log "Checking Docker Compose configuration"
docker compose version
docker compose config --quiet
docker compose run --rm --no-deps gateway \
  --mode validate -c /etc/envoy/envoy.yaml >/dev/null

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

idempotency_has_entity="$(
  docker compose exec -T db psql -U erp_user -d erp_db -Atc \
    "SELECT EXISTS (
       SELECT 1
       FROM information_schema.columns
       WHERE table_schema = 'public'
         AND table_name = 'idempotency_key'
         AND column_name = 'entity_id'
     );"
)"
if [ "$idempotency_has_entity" != "t" ]; then
  docker compose exec -T db psql -U erp_user -d erp_db \
    -v ON_ERROR_STOP=1 \
    -f /docker-entrypoint-initdb.d/005_entity_scoped_idempotency.sql
else
  echo "Migration 005 already present"
fi

fx_import_table="$(
  docker compose exec -T db psql -U erp_user -d erp_db -Atc \
    "SELECT COALESCE(to_regclass('public.fx_import_job')::text, '');"
)"
if [ "$fx_import_table" != "fx_import_job" ]; then
  docker compose exec -T db psql -U erp_user -d erp_db \
    -v ON_ERROR_STOP=1 \
    -f /docker-entrypoint-initdb.d/006_fx_rate_ingestion.sql
else
  echo "Migration 006 already present"
fi

aging_uses_base_currency="$(
  docker compose exec -T db psql -U erp_user -d erp_db -Atc \
    "SELECT POSITION(
       'base_balance_amount' IN pg_get_viewdef('ar_aging'::regclass, true)
     ) > 0;"
)"
if [ "$aging_uses_base_currency" != "t" ]; then
  docker compose exec -T db psql -U erp_user -d erp_db \
    -v ON_ERROR_STOP=1 \
    -f /docker-entrypoint-initdb.d/007_base_currency_ar_aging.sql
else
  echo "Migration 007 already present"
fi

base_only_fx_lines="$(
  docker compose exec -T db psql -U erp_user -d erp_db -Atc \
    "SELECT EXISTS (
       SELECT 1 FROM pg_constraint
       WHERE conrelid = 'journal_entry_line'::regclass
         AND conname = 'je_line_base_double_entry'
     );"
)"
if [ "$base_only_fx_lines" != "t" ]; then
  docker compose exec -T db psql -U erp_user -d erp_db \
    -v ON_ERROR_STOP=1 \
    -f /docker-entrypoint-initdb.d/008_base_only_fx_journal_lines.sql
else
  echo "Migration 008 already present"
fi

log "Refreshing the aging snapshot for deterministic health verification"
docker compose exec -T db psql -U erp_user -d erp_db \
  -v ON_ERROR_STOP=1 \
  -c "REFRESH MATERIALIZED VIEW CONCURRENTLY ar_aging" >/dev/null

log "Starting the internal AR application, workers, and L7 gateway"
docker compose up "${up_options[@]}" app delivery_worker fx_rate_worker gateway

log "Waiting for service health checks"
wait_for_url "http://localhost:9000/health" "Delivery stub"
wait_for_url "http://localhost:8000/health" "Envoy gateway and AR application"

gateway_container="$(docker compose ps --status running --quiet gateway)"
if [ -z "$gateway_container" ]; then
  echo "Envoy gateway is not running" >&2
  exit 1
fi

published_app_port="$(
  docker inspect erp_app \
    --format '{{json (index .NetworkSettings.Ports "8080/tcp")}}'
)"
if [ "$published_app_port" != "null" ]; then
  echo "AR application must not publish a host port: $published_app_port" >&2
  exit 1
fi

worker_container="$(docker compose ps --status running --quiet delivery_worker)"
if [ -z "$worker_container" ]; then
  echo "Delivery worker is not running" >&2
  exit 1
fi

fx_worker_container="$(docker compose ps --status running --quiet fx_rate_worker)"
if [ -z "$fx_worker_container" ]; then
  echo "FX rate worker is not running" >&2
  exit 1
fi

if [ "$RUN_SEED" = true ]; then
  log "Seeding deterministic demonstration data"
  docker compose exec -T app python -m src.seed_data
fi

if [ "$RUN_TESTS" = true ]; then
  log "Running integration tests"
  ./test_api.sh
  ./test_payment_concurrency.sh
fi

log "Deployment successful"
docker compose ps
echo
echo "L7 Gateway:    http://localhost:8000"
echo "AR API:        internal app:8080 (not host-published)"
echo "API docs:      http://localhost:8000/docs"
echo "Delivery stub: http://localhost:9000"
echo "Live delivery: docker compose logs -f stub delivery_worker"
echo "Live FX feed:  docker compose logs -f fx_rate_worker"
