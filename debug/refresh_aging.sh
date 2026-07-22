#!/bin/bash
# Manually refresh the ar_aging materialized view (normally refreshed every 5 min via pg_cron).
# Usage: ./refresh_aging.sh

PROJECT=/Users/pankajkapoor/projects/erp-ar-module

cd "$PROJECT" || exit 1

echo ""
echo "=== Refreshing ar_aging materialized view ==="
docker compose exec -T db psql -U erp_user -d erp_db -c "REFRESH MATERIALIZED VIEW CONCURRENTLY ar_aging;"
