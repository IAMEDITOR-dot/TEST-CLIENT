#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -ne 1 ]]; then
  echo "Usage: ./create-admin.sh USERNAME" >&2
  exit 2
fi

docker compose --env-file "$ROOT_DIR/vm-data/guac.env" -f "$ROOT_DIR/docker-compose.yml" exec manager \
  python3 /app/server.py create-admin --username "$1"
