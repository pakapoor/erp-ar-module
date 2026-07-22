#!/bin/bash
# Tail live logs from the delivery stub container to watch delivery events as they arrive.
# Usage: ./tail_stub.sh

PROJECT=/Users/pankajkapoor/projects/erp-ar-module

cd "$PROJECT" || exit 1

echo ""
echo "=== Tailing erp_stub logs (Ctrl+C to stop) ==="
echo ""

docker compose logs stub -f
