#!/bin/bash
set -e

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname postgres <<-'SQL'
CREATE EXTENSION IF NOT EXISTS pg_cron;

SELECT cron.schedule_in_database(
    'refresh-ar-aging-every-5-minutes',
    '*/5 * * * *',
    'REFRESH MATERIALIZED VIEW CONCURRENTLY ar_aging',
    'erp_db'
)
WHERE NOT EXISTS (
    SELECT 1
    FROM cron.job
    WHERE jobname = 'refresh-ar-aging-every-5-minutes'
);
SQL
