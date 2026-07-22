#!/bin/bash
# Start FastAPI app in debugpy mode
# Run this first, then attach VSCode with F5 (Debug FastAPI in Docker)

echo "=== Stopping existing app container ==="
cd "$(dirname "$0")/.."
docker compose stop app

echo ""
echo "=== Starting app with debugpy on port 5678 ==="
echo "Now attach VSCode: Cmd+Shift+D -> Debug FastAPI in Docker -> F5"
echo ""

docker compose run --rm -p 5678:5678 -p 8080:8080 app 
  python -m debugpy --listen 0.0.0.0:5678 --wait-for-client 
  -m uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload
