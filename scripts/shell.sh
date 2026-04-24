#!/usr/bin/env bash
# drop into the running vivarium container.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# start it if it isn't running yet
if ! docker compose ps --status running --services 2>/dev/null | grep -q vivarium; then
  echo "[shell] vivarium isn't running. starting it."
  docker compose up -d
fi

exec docker compose exec vivarium bash
