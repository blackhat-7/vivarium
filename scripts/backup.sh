#!/usr/bin/env bash
# host-side backup of vivarium's work dir. runs on the host, not in the container.
# install as a cron: 0 */2 * * * bash ~/vivarium/scripts/backup.sh >> ~/vivarium-backup.log 2>&1

set -euo pipefail

SRC="${VIVARIUM_HOME:-$HOME/vivarium-home}/work"
DEST_ROOT="${VIVARIUM_BACKUP:-$HOME/vivarium-backup}"
HOUR_SLOT="$DEST_ROOT/hourly-$(date +%H)"
DAY_SLOT="$DEST_ROOT/daily-$(date +%u)"

mkdir -p "$DEST_ROOT"

if [[ ! -d "$SRC" ]] || [[ -z "$(ls -A "$SRC" 2>/dev/null)" ]]; then
  echo "[$(date -Iseconds)] work dir empty; skipping"
  exit 0
fi

echo "[$(date -Iseconds)] snapshotting $SRC -> $HOUR_SLOT"
rsync -a --delete \
  --exclude='node_modules' --exclude='.venv' --exclude='target' \
  --exclude='__pycache__' --exclude='.next' --exclude='dist' --exclude='build' \
  "$SRC/" "$HOUR_SLOT/"

# once a day, keep a day-of-week slot — 7 days of daily history, near-zero extra
# disk via hardlinks against the matching hourly.
if [[ "$(date +%H)" == "03" ]]; then
  echo "[$(date -Iseconds)] daily snapshot -> $DAY_SLOT"
  rsync -a --delete --link-dest="$HOUR_SLOT" "$HOUR_SLOT/" "$DAY_SLOT/"
fi

# once a week (Sunday 03:00), keep a rolling 8-week slot. Catches a slow
# compromise that has poisoned the daily lineage before discovery —
# without this, a >7-day quiet compromise leaves no clean ground to
# restore from. 10# forces base-10 to handle leading-zero week numbers.
if [[ "$(date +%H)" == "03" ]] && [[ "$(date +%u)" == "7" ]]; then
  WEEK_SLOT="$DEST_ROOT/weekly-$(( 10#$(date +%V) % 8 + 1 ))"
  echo "[$(date -Iseconds)] weekly snapshot -> $WEEK_SLOT"
  rsync -a --delete --link-dest="$HOUR_SLOT" "$HOUR_SLOT/" "$WEEK_SLOT/"
fi

echo "[$(date -Iseconds)] done"
