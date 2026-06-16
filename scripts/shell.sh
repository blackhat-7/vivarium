#!/usr/bin/env bash
# drop into the running vivarium container.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
profile_arg="${1:-}"
if [[ $# -gt 1 ]]; then
  echo "usage: $0 [profile-name|env-file]" >&2
  exit 1
fi
# shellcheck disable=SC1091
. ./scripts/profile.sh "$profile_arg"

# start it if it isn't running yet
started=false
if ! vivarium_compose ps --status running --services 2>/dev/null | grep -q vivarium; then
  echo "[shell] $COMPOSE_PROJECT_NAME isn't running. starting it."
  "./scripts/up.sh" ${profile_arg:+"$profile_arg"}
  started=true
fi

# If the container was already running, re-prime HTTPS clone credentials before
# opening a shell. When this script started the container, up.sh already did it.
if [[ "$started" != true && -n "${GITHUB_READ_TOKEN:-}" ]]; then
  ./scripts/git-auth.sh ${profile_arg:+"$profile_arg"}
fi

# Do not pass the clone token through as normal process/container environment.
export -n GITHUB_READ_TOKEN 2>/dev/null || true
unset GITHUB_READ_TOKEN

vivarium_compose exec vivarium bash
