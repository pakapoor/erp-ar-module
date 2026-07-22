#!/bin/bash
# Stop debug mode and restart app normally

cd "$(dirname "$0")/.."

echo "=== Stopping app in debug mode ==="
docker compose stop app

echo "=== Restarting app in normal mode ==="
docker compose up app -d

echo "=== Done ==="
