#!/usr/bin/env bash
# build + start the vivarium container. idempotent.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

VIVARIUM_HOME="${VIVARIUM_HOME:-$HOME/vivarium-home}"
mkdir -p "$VIVARIUM_HOME"

# Upsert keys in .env — preserve user edits, add anything missing with defaults.
touch .env
upsert_env() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" .env; then
    return 0
  fi
  printf '%s=%s\n' "$key" "$value" >> .env
}
# HOST_UID/GID always sync to current user (so running as a different host user works)
if grep -qE '^HOST_UID=' .env; then
  sed -i.bak "s/^HOST_UID=.*/HOST_UID=$(id -u)/" .env && rm -f .env.bak
else
  echo "HOST_UID=$(id -u)" >> .env
fi
if grep -qE '^HOST_GID=' .env; then
  sed -i.bak "s/^HOST_GID=.*/HOST_GID=$(id -g)/" .env && rm -f .env.bak
else
  echo "HOST_GID=$(id -g)" >> .env
fi
upsert_env VIVARIUM_HOME "$VIVARIUM_HOME"
upsert_env INSTALL_OPENCODE true
upsert_env INSTALL_CLAUDE false

echo "[up] current agent selection:"
grep -E '^INSTALL_(OPENCODE|CLAUDE)=' .env | sed 's/^/  /' || true

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
