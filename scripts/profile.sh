#!/usr/bin/env bash
# Resolve a vivarium profile argument to env/project/container defaults.
# Source this from scripts after cd'ing to the repo root.

profile_arg="${1:-}"
VIVARIUM_ROOT="$(pwd)"
VIVARIUM_PROFILE_EXTERNAL=false

if [[ -z "$profile_arg" ]]; then
  VIVARIUM_PROFILE=default
  VIVARIUM_ENV_FILE="$VIVARIUM_ROOT/.env"
elif [[ "$profile_arg" == */* || "$profile_arg" == *.env ]]; then
  VIVARIUM_PROFILE_EXTERNAL=true
  VIVARIUM_ENV_FILE="$profile_arg"
  [[ "$VIVARIUM_ENV_FILE" = /* ]] || VIVARIUM_ENV_FILE="$VIVARIUM_ROOT/$VIVARIUM_ENV_FILE"
  [[ -f "$VIVARIUM_ENV_FILE" ]] || { echo "[profile] missing env file: $VIVARIUM_ENV_FILE" >&2; exit 1; }
  VIVARIUM_PROFILE="$(basename "$VIVARIUM_ENV_FILE" .env)"
else
  [[ "$profile_arg" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || {
    echo "[profile] invalid profile name '$profile_arg' (use lowercase letters, numbers, _ or -)" >&2
    exit 1
  }
  VIVARIUM_PROFILE="$profile_arg"
  VIVARIUM_ENV_FILE="$VIVARIUM_ROOT/profiles/$VIVARIUM_PROFILE.env"
fi

mkdir -p "$(dirname "$VIVARIUM_ENV_FILE")"
$VIVARIUM_PROFILE_EXTERNAL || touch "$VIVARIUM_ENV_FILE"

if [[ -f "$VIVARIUM_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$VIVARIUM_ENV_FILE"
  set +a
fi

if [[ "$VIVARIUM_PROFILE" == default ]]; then
  default_project=vivarium
  default_home="$HOME/vivarium-home"
  default_backup="$HOME/vivarium-backup"
else
  default_project="vivarium-$VIVARIUM_PROFILE"
  default_home="$HOME/vivarium-home-$VIVARIUM_PROFILE"
  default_backup="$HOME/vivarium-backup-$VIVARIUM_PROFILE"
fi

export VIVARIUM_ROOT VIVARIUM_PROFILE VIVARIUM_ENV_FILE VIVARIUM_PROFILE_EXTERNAL
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$default_project}"
export CONTAINER_NAME="${CONTAINER_NAME:-$COMPOSE_PROJECT_NAME}"
export VIVARIUM_HOME="${VIVARIUM_HOME:-$default_home}"
export VIVARIUM_BACKUP="${VIVARIUM_BACKUP:-$default_backup}"

# One optional host broker is shared by all profiles. Its config and credentials
# never enter the container; Compose receives only the request socket path.
PUSH_GATE_CONFIG_FILE="$HOME/.config/vivarium/push-gate.env"
PUSH_GATE_HOST_ENABLED=false
PUSH_GATE_SOCKET_DIR="$HOME/.local/share/vivarium-push-gate"
if [[ -f "$PUSH_GATE_CONFIG_FILE" ]]; then
  [[ "$(stat -c '%a:%u' "$PUSH_GATE_CONFIG_FILE" 2>/dev/null)" == "600:$(id -u)" ]] || {
    echo "[profile] $PUSH_GATE_CONFIG_FILE must be owned by you with mode 0600" >&2
    exit 1
  }
  grep -qx 'PUSH_GATE_ENABLE=true' "$PUSH_GATE_CONFIG_FILE" && PUSH_GATE_HOST_ENABLED=true
fi
export PUSH_GATE_CONFIG_FILE PUSH_GATE_SOCKET_DIR

vivarium_compose() {
  local files=(-f "$VIVARIUM_ROOT/compose.yaml")
  $PUSH_GATE_HOST_ENABLED && files+=(-f "$VIVARIUM_ROOT/compose.push-gate.yaml")
  docker compose "${files[@]}" --env-file "$VIVARIUM_ENV_FILE" -p "$COMPOSE_PROJECT_NAME" "$@"
}

resolve_build_ref() {
  local env_key="$1" repo="$2" label="$3" ref sha
  ref="${!env_key:-}"
  [[ -n "$ref" ]] || return 0

  if [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
    echo "[build] $label pinned to ${ref:0:12}"
    return 0
  fi

  sha="$(git ls-remote "$repo" "refs/heads/$ref" 2>/dev/null | awk 'NF {print $1; exit}')"
  [[ -n "$sha" ]] || sha="$(git ls-remote "$repo" "refs/tags/$ref^{}" 2>/dev/null | awk 'NF {print $1; exit}')"
  [[ -n "$sha" ]] || sha="$(git ls-remote "$repo" "refs/tags/$ref" 2>/dev/null | awk 'NF {print $1; exit}')"
  [[ -n "$sha" ]] || sha="$(git ls-remote "$repo" "$ref" 2>/dev/null | awk 'NF {print $1; exit}')"

  if [[ -z "$sha" ]]; then
    echo "[FATAL] could not resolve $label ref '$ref' from $repo" >&2
    echo "[FATAL] check network access or pin $env_key to a full commit SHA in $VIVARIUM_ENV_FILE" >&2
    exit 1
  fi

  echo "[build] $label $ref -> ${sha:0:12}"
  export "$env_key=$sha"
}

resolve_latest_release() {
  local env_key="$1" repo="$2" label="$3" version
  version="$(
    git ls-remote --tags --refs "$repo" 'refs/tags/v*' 2>/dev/null \
      | awk -F/ '$3 ~ /^v[0-9]+\.[0-9]+\.[0-9]+$/ {sub(/^v/, "", $3); print $3}' \
      | sort -V \
      | tail -1
  )"
  if [[ -z "$version" ]]; then
    echo "[FATAL] could not resolve the latest $label release from $repo" >&2
    echo "[FATAL] check network access to resolve the release used for this build" >&2
    exit 1
  fi

  echo "[build] $label latest -> $version"
  export "$env_key=$version"
}
