#!/usr/bin/env bash
# build + start the vivarium container. idempotent.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# BuildKit: needed for the apt cache-mount in the Dockerfile and for the
# cache-key behavior that makes ARG reordering actually pay off (a busted
# late ARG no longer invalidates earlier layers).
export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

# Dedicated buildx builder pinned to host networking. The default
# docker-container builder runs in an isolated netns whose resolver
# breaks `apt-get update` on some hosts (Temporary failure resolving
# 'archive.ubuntu.com'). Bootstrap is one-time (~5s); reused after that.
if docker buildx version >/dev/null 2>&1; then
  if ! docker buildx inspect vivarium-builder >/dev/null 2>&1; then
    echo "[up] creating dedicated buildx builder (one-time, ~5s)"
    docker buildx create --name vivarium-builder \
      --driver docker-container --driver-opt network=host \
      --bootstrap >/dev/null
  fi
  export BUILDX_BUILDER=vivarium-builder
fi

VIVARIUM_HOME="${VIVARIUM_HOME:-$HOME/vivarium-home}"
# create both the home and the work dir on the host BEFORE the bind mount is
# established. This guarantees they exist with the host user's ownership so
# the in-container vivarium user (matching UID) can write to them.
mkdir -p "$VIVARIUM_HOME/work"

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
upsert_env INSTALL_BESTIARY false
upsert_env BESTIARY_REF main

echo "[up] current agent selection:"
grep -E '^INSTALL_(OPENCODE|CLAUDE|BESTIARY)=' .env | sed 's/^/  /' || true

# fail fast if no agent CLI is selected — same check that used to live as a
# RUN step in the Dockerfile (moved here so it doesn't bust the apt cache).
if ! grep -qE '^INSTALL_OPENCODE=true$' .env \
   && ! grep -qE '^INSTALL_CLAUDE=true$'  .env; then
  echo "[FATAL] at least one of INSTALL_OPENCODE / INSTALL_CLAUDE must be true in .env" >&2
  exit 1
fi

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
