#!/usr/bin/env bash
# install backup/audit cron entries for a vivarium profile.
# idempotent for that profile; leaves unrelated crontab entries alone.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
profile_arg="${1:-}"
if [[ $# -gt 1 ]]; then
  echo "usage: $0 [profile-name|env-file]" >&2
  exit 1
fi
# shellcheck disable=SC1091
. ./scripts/profile.sh "$profile_arg"

arg="${profile_arg:+ $profile_arg}"
tag="# vivarium:$VIVARIUM_PROFILE"
backup_log="$HOME/vivarium-backup-$VIVARIUM_PROFILE.log"
audit_log="$HOME/vivarium-audit-$VIVARIUM_PROFILE.log"
BACKUP_LINE="0 */2 * * * bash $VIVARIUM_ROOT/scripts/backup.sh$arg >> $backup_log 2>&1 $tag"
AUDIT_LINE="0 9 1 * * bash $VIVARIUM_ROOT/scripts/audit.sh$arg > $audit_log 2>&1 $tag"

OTHER=$(crontab -l 2>/dev/null | grep -vF "$tag" || true)
{
  [ -n "$OTHER" ] && printf '%s\n' "$OTHER"
  printf '%s\n' "$BACKUP_LINE"
  printf '%s\n' "$AUDIT_LINE"
} | crontab -

echo "[cron-install] active entries for $VIVARIUM_PROFILE:"
crontab -l | grep -F "$tag" | sed 's/^/  /'
