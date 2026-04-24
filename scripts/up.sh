#!/usr/bin/env bash
# build + start the vivarium container. idempotent.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

VIVARIUM_HOME="${VIVARIUM_HOME:-$HOME/vivarium-home}"
mkdir -p "$VIVARIUM_HOME"

# Create .env with defaults only if it doesn't exist. After that, edits to
# .env are respected — edit INSTALL_OPENCODE / INSTALL_CLAUDE to change the
# agent set, then re-run this script to rebuild.
if [ ! -f .env ]; then
  cat > .env <<EOF
HOST_UID=$(id -u)
HOST_GID=$(id -g)
VIVARIUM_HOME=$VIVARIUM_HOME
INSTALL_OPENCODE=true
INSTALL_CLAUDE=false
EOF
  echo "[up] created .env with defaults (opencode only). edit to change agents."
else
  # keep HOST_UID/GID in sync in case the user who runs this changed
  sed -i.bak "s/^HOST_UID=.*/HOST_UID=$(id -u)/" .env
  sed -i.bak "s/^HOST_GID=.*/HOST_GID=$(id -g)/" .env
  rm -f .env.bak
fi

echo "[up] current agent selection:"
grep -E '^INSTALL_(OPENCODE|CLAUDE)=' .env | sed 's/^/  /'

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
