#!/usr/bin/env bash
# remove vivarium cron entries. leaves backup directory + logs alone.
# idempotent — safe to run when nothing is installed.

set -euo pipefail

BEFORE=$(crontab -l 2>/dev/null | grep -c 'vivarium/scripts' || true)
OTHER=$(crontab -l 2>/dev/null | grep -v 'vivarium/scripts' || true)

if [ -z "$OTHER" ]; then
  crontab -r 2>/dev/null || true
else
  printf '%s\n' "$OTHER" | crontab -
fi

REMAINING=$(crontab -l 2>/dev/null | grep -c 'vivarium/scripts' || true)
echo "[cron-uninstall] removed $((BEFORE - REMAINING)) vivarium entries"
echo "[cron-uninstall] remaining crontab:"
if crontab -l >/dev/null 2>&1; then
  crontab -l | sed 's/^/  /'
else
  echo "  (crontab is now empty)"
fi
