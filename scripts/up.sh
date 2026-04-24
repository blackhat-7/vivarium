#!/usr/bin/env bash
# build + start the vivarium container. idempotent.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

VIVARIUM_HOME="${VIVARIUM_HOME:-$HOME/vivarium-home}"
mkdir -p "$VIVARIUM_HOME"

# write .env with host UID/GID so the container user matches file ownership.
# overwrites on each run — harmless, just keeps it in sync if your UID changes.
cat > .env <<EOF
HOST_UID=$(id -u)
HOST_GID=$(id -g)
VIVARIUM_HOME=$VIVARIUM_HOME
EOF

echo "[up] building image (first time: ~3 min; cached: seconds)"
docker compose build

echo "[up] starting container"
docker compose up -d

echo "[up] container state:"
docker compose ps

cat <<EOF

[up] done. shell in with:  $(dirname "${BASH_SOURCE[0]}")/shell.sh

first time? you probably want to run inside the container:
  opencode auth login        # pick your subscription provider

then clone a repo into ~/work and get going.
EOF
