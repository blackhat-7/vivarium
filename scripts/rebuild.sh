#!/usr/bin/env bash
# Rebuild the vivarium image and recreate the container without deleting data.
#
# DEFAULT: resolves moving build refs (ai-harnesses/bestiary branches or tags)
# to commit SHAs so Docker cache invalidates when upstream moves, then rebuilds
# with normal layer cache and recreates the container. The bind-mounted
# VIVARIUM_HOME is preserved.
#
# --no-cache / --fresh: rebuild every Docker layer too. Still preserves data.
# [profile|env-file]: optional target profile/env file.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

NO_CACHE=false
profile_arg=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-cache|--fresh) NO_CACHE=true ;;
    -h|--help)
      awk '/^# /{sub(/^# ?/,""); print; next} /^[^#]/{exit}' "$0"
      exit 0
      ;;
    --*) echo "unknown flag: $1" >&2; exit 1 ;;
    *)
      [[ -z "$profile_arg" ]] || { echo "multiple profiles/env files provided" >&2; exit 1; }
      profile_arg="$1"
      ;;
  esac
  shift
done

# shellcheck disable=SC1091
. ./scripts/profile.sh "$profile_arg"

# Preserve an externally supplied token for git-auth.sh, but keep it out of
# docker compose's build/up environment.
github_read_token="${GITHUB_READ_TOKEN:-}"
export -n GITHUB_READ_TOKEN 2>/dev/null || true
unset GITHUB_READ_TOKEN

# Revalidate/recreate the shared external gate before replacing the profile.
if $EXTERNAL_GATE_HOST_ENABLED; then
  ./scripts/external-gate.sh start
fi

export HOST_UID="$(id -u)"
export HOST_GID="$(id -g)"
export INSTALL_OPENCODE="${INSTALL_OPENCODE:-true}"
export INSTALL_CLAUDE="${INSTALL_CLAUDE:-false}"
export INSTALL_PASEO="${INSTALL_PASEO:-false}"
export INSTALL_BESTIARY="${INSTALL_BESTIARY:-false}"
export BESTIARY_REF="${BESTIARY_REF:-main}"
export AI_HARNESSES_REF="${AI_HARNESSES_REF:-main}"
export AI_HARNESSES_MCP="${AI_HARNESSES_MCP:-none}"
export OPENCODE_WEB_ENABLE="${OPENCODE_WEB_ENABLE:-true}"
export OPENCODE_WEB_PORT="${OPENCODE_WEB_PORT:-4096}"

# BuildKit: needed for the apt cache-mount in the Dockerfile.
export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

# Dedicated buildx builder pinned to host networking. Mirrors scripts/up.sh.
if docker buildx version >/dev/null 2>&1; then
  if ! docker buildx inspect vivarium-builder >/dev/null 2>&1; then
    echo "[rebuild] creating dedicated buildx builder (one-time, ~5s)"
    docker buildx create --name vivarium-builder \
      --driver docker-container --driver-opt network=host \
      --bootstrap >/dev/null
  fi
  export BUILDX_BUILDER=vivarium-builder
fi

mkdir -p "$VIVARIUM_HOME/work"

if [[ "$INSTALL_OPENCODE" != true && "$INSTALL_CLAUDE" != true ]]; then
  echo "[FATAL] at least one of INSTALL_OPENCODE / INSTALL_CLAUDE must be true in $VIVARIUM_ENV_FILE" >&2
  exit 1
fi

if [[ "$INSTALL_BESTIARY" == true ]]; then
  resolve_build_ref BESTIARY_REF https://github.com/blackhat-7/bestiary.git bestiary
fi
resolve_build_ref AI_HARNESSES_REF https://github.com/blackhat-7/ai-harnesses.git ai-harnesses
resolve_latest_release PI_VERSION https://github.com/earendil-works/pi.git Pi

echo "[rebuild] profile: $VIVARIUM_PROFILE ($VIVARIUM_ENV_FILE)"
echo "[rebuild] preserving bind mount: $VIVARIUM_HOME"
echo "[rebuild] in-container processes will restart: opencode, paseo, tmux, shells"

build_args=(--pull)
if $NO_CACHE; then
  build_args+=(--no-cache)
  echo "[rebuild] mode: no-cache / fresh image rebuild"
else
  echo "[rebuild] mode: cached rebuild with moving refs cache-busted"
fi

echo "[rebuild] building image"
vivarium_compose build "${build_args[@]}" vivarium

echo "[rebuild] recreating container"
vivarium_compose up -d --force-recreate

# Re-apply global rules explicitly, matching up.sh. Entrypoint also does this
# on recreation, but this keeps behavior consistent if compose ever reuses.
echo "[rebuild] reapplying global agent rules"
vivarium_compose exec -T vivarium sh -lc '
  src=/opt/vivarium/skel/AGENTS.md
  [ -f "$src" ] || exit 0
  install -D -m 0644 "$src" "$HOME/.pi/agent/AGENTS.md"
  install -D -m 0644 "$src" "$HOME/.config/opencode/AGENTS.md"
  install -D -m 0644 "$src" "$HOME/.claude/CLAUDE.md"
'

if [[ -n "$github_read_token" ]]; then
  GITHUB_READ_TOKEN="$github_read_token" ./scripts/git-auth.sh ${profile_arg:+"$profile_arg"}
else
  ./scripts/git-auth.sh ${profile_arg:+"$profile_arg"}
fi

echo "[rebuild] container state:"
vivarium_compose ps

echo
echo "[rebuild] done. shell in with: ./scripts/shell.sh${profile_arg:+ $profile_arg}"
