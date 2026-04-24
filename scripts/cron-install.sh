#!/usr/bin/env bash
# install vivarium cron entries on the host:
#   - backup every 2 hours
#   - audit on the 1st of each month
# idempotent — re-running replaces any existing vivarium entries, leaves others alone.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIVARIUM_DIR="$(dirname "$HERE")"

BACKUP_LINE="0 */2 * * * bash $VIVARIUM_DIR/scripts/backup.sh >> $HOME/vivarium-backup.log 2>&1"
AUDIT_LINE="0 9 1 * * bash $VIVARIUM_DIR/scripts/audit.sh > $HOME/vivarium-audit.log 2>&1"

OTHER=$(crontab -l 2>/dev/null | grep -v 'vivarium/scripts' || true)

{
  [ -n "$OTHER" ] && printf '%s\n' "$OTHER"
  printf '%s\n' "$BACKUP_LINE"
  printf '%s\n' "$AUDIT_LINE"
} | crontab -

echo "[cron-install] active vivarium entries:"
crontab -l | grep vivarium/scripts | sed 's/^/  /'
