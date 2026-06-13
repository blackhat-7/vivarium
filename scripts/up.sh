#!/usr/bin/env bash
# build + start the vivarium container. idempotent.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
profile_arg="${1:-}"
if [[ $# -gt 1 ]]; then
  echo "usage: $0 [profile-name|env-file]" >&2
  exit 1
fi
# shellcheck disable=SC1091
. ./scripts/profile.sh "$profile_arg"

# Optional clone credential. Keep it as a shell-local variable so docker compose
# does not pass it through as a normal container environment variable.
github_read_token="${GITHUB_READ_TOKEN:-}"
export -n GITHUB_READ_TOKEN 2>/dev/null || true
unset GITHUB_READ_TOKEN

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

# create both the home and the work dir on the host BEFORE the bind mount is
# established. This guarantees they exist with the host user's ownership so
# the in-container vivarium user (matching UID) can write to them.
mkdir -p "$VIVARIUM_HOME/work"

# Upsert keys in the profile env file — preserve user edits, add missing defaults.
set_env() {
  local key="$1" value="$2"
  export "$key=$value"
  $VIVARIUM_PROFILE_EXTERNAL && return 0
  if grep -qE "^${key}=" "$VIVARIUM_ENV_FILE"; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$VIVARIUM_ENV_FILE" && rm -f "$VIVARIUM_ENV_FILE.bak"
  else
    printf '%s=%s\n' "$key" "$value" >> "$VIVARIUM_ENV_FILE"
  fi
}
upsert_env() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$VIVARIUM_ENV_FILE" 2>/dev/null; then
    return 0
  fi
  export "$key=${!key:-$value}"
  $VIVARIUM_PROFILE_EXTERNAL || printf '%s=%s\n' "$key" "${!key}" >> "$VIVARIUM_ENV_FILE"
}
# HOST_UID/GID always sync to current user (so running as a different host user works)
set_env HOST_UID "$(id -u)"
set_env HOST_GID "$(id -g)"
upsert_env COMPOSE_PROJECT_NAME "$COMPOSE_PROJECT_NAME"
upsert_env CONTAINER_NAME "$CONTAINER_NAME"
upsert_env VIVARIUM_HOME "$VIVARIUM_HOME"
upsert_env VIVARIUM_BACKUP "$VIVARIUM_BACKUP"
upsert_env INSTALL_OPENCODE true
upsert_env INSTALL_CLAUDE false
upsert_env INSTALL_PASEO false
upsert_env INSTALL_BESTIARY false
upsert_env BESTIARY_REF main
upsert_env AI_HARNESSES_REF main
upsert_env AI_HARNESSES_MCP none

echo "[up] profile: $VIVARIUM_PROFILE ($VIVARIUM_ENV_FILE)"
echo "[up] current agent selection:"
printf '  INSTALL_OPENCODE=%s\n  INSTALL_CLAUDE=%s\n  INSTALL_PASEO=%s\n  INSTALL_BESTIARY=%s\n' \
  "${INSTALL_OPENCODE:-}" "${INSTALL_CLAUDE:-}" "${INSTALL_PASEO:-}" "${INSTALL_BESTIARY:-}"
echo "[up] current AI harness selection:"
printf '  AI_HARNESSES_REF=%s\n  AI_HARNESSES_MCP=%s\n' "${AI_HARNESSES_REF:-}" "${AI_HARNESSES_MCP:-}"

# fail fast if no agent CLI is selected — same check that used to live as a
# RUN step in the Dockerfile (moved here so it doesn't bust the apt cache).
if [[ "${INSTALL_OPENCODE:-}" != true && "${INSTALL_CLAUDE:-}" != true ]]; then
  echo "[FATAL] at least one of INSTALL_OPENCODE / INSTALL_CLAUDE must be true in $VIVARIUM_ENV_FILE" >&2
  exit 1
fi

echo "[up] building image (first time: ~3 min; cached: seconds)"
vivarium_compose build

echo "[up] starting container"
vivarium_compose up -d

if [[ -n "$github_read_token" ]]; then
  echo "[up] priming GitHub HTTPS read credential for normal git clone"
  printf 'protocol=https\nhost=github.com\nusername=x-access-token\npassword=%s\n\n' \
    "$github_read_token" \
    | vivarium_compose exec -T vivarium git credential approve >/dev/null
  unset github_read_token
fi

echo "[up] container state:"
vivarium_compose ps

cat <<EOF

[up] done. shell in with:  $(dirname "${BASH_SOURCE[0]}")/shell.sh${profile_arg:+ $profile_arg}

first time? you probably want to run inside the container:
  opencode auth login        # pick your subscription provider

then clone a repo into ~/work and get going.
EOF
