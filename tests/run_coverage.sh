#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

docker compose exec -T app sh -c '
  coverage erase
  coverage run -m unittest discover -s tests/unit -t .
  coverage report
'
