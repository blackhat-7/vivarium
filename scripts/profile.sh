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

vivarium_compose() {
  docker compose --env-file "$VIVARIUM_ENV_FILE" -p "$COMPOSE_PROJECT_NAME" "$@"
}
