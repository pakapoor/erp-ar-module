#!/bin/bash
# Start FastAPI app in debugpy mode
# Run this first, then attach VSCode with F5 (Debug FastAPI in Docker)

cd "$(dirname "$0")/.."

echo "=== Restarting app in debug mode ==="
docker compose stop app
docker compose -f docker-compose.yml -f docker-compose.debug.yml up app -d

echo ""
echo "=== Waiting for debugpy to be ready ==="
sleep 2
echo "Now attach VSCode: Cmd+Shift+D -> Debug FastAPI in Docker -> F5"
