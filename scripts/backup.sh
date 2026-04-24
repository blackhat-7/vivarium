#!/usr/bin/env bash
# vivarium backup — snapshot ~/work into ~/backup/work-HH/.
# Runs every 2 hours via cron. Keeps 7 days of hourly slots (24 * 7 = 168 max,
# but we key by HH so there are 12 slots that get overwritten each day).
# For longer retention: daily slots kept for 7 days.

set -euo pipefail

WORK="$HOME/work"
BACKUP="$HOME/backup"
HOUR_SLOT="$BACKUP/work-$(date +%H)"
DAY_SLOT="$BACKUP/daily-$(date +%u)"   # 1-7

mkdir -p "$BACKUP"

if [[ ! -d "$WORK" ]] || [[ -z "$(ls -A "$WORK" 2>/dev/null)" ]]; then
  echo "[$(date -Iseconds)] work dir empty; skipping"
  exit 0
fi

echo "[$(date -Iseconds)] snapshotting to $HOUR_SLOT"
rsync -a --delete --exclude='node_modules' --exclude='.venv' --exclude='target' \
  --exclude='__pycache__' --exclude='.next' --exclude='dist' --exclude='build' \
  "$WORK/" "$HOUR_SLOT/"

# at 03:00 also write the day-of-week slot for 7-day retention
if [[ "$(date +%H)" == "03" ]]; then
  echo "[$(date -Iseconds)] daily snapshot to $DAY_SLOT"
  rsync -a --delete --link-dest="$HOUR_SLOT" "$HOUR_SLOT/" "$DAY_SLOT/"
fi

echo "[$(date -Iseconds)] done"
