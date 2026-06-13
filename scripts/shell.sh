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
if ! vivarium_compose ps --status running --services 2>/dev/null | grep -q vivarium; then
  echo "[shell] $COMPOSE_PROJECT_NAME isn't running. starting it."
  "./scripts/up.sh" ${profile_arg:+"$profile_arg"}
fi

exec vivarium_compose exec vivarium bash
