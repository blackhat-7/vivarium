#!/usr/bin/env bash
# remove vivarium cron entries. leaves backup directory + logs alone.
# with a profile arg, removes only that profile; with no arg, removes all legacy/profile entries.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
profile_arg="${1:-}"
if [[ $# -gt 1 ]]; then
  echo "usage: $0 [profile-name|env-file]" >&2
  exit 1
fi

if [[ -n "$profile_arg" ]]; then
  # shellcheck disable=SC1091
  . ./scripts/profile.sh "$profile_arg"
  pattern="# vivarium:$VIVARIUM_PROFILE"
else
  pattern='vivarium/scripts|# vivarium:'
fi

BEFORE=$(crontab -l 2>/dev/null | grep -Ec "$pattern" || true)
OTHER=$(crontab -l 2>/dev/null | grep -Ev "$pattern" || true)

if [ -z "$OTHER" ]; then
  crontab -r 2>/dev/null || true
else
  printf '%s\n' "$OTHER" | crontab -
fi

REMAINING=$(crontab -l 2>/dev/null | grep -Ec "$pattern" || true)
echo "[cron-uninstall] removed $((BEFORE - REMAINING)) vivarium entries"
