#!/usr/bin/env bash
# remove vivarium from the host.
#
# DEFAULT (no flags):    stop + remove container, remove image, uninstall cron.
#                        your code + auth in ~/vivarium-home is PRESERVED.
#                        your backups in ~/vivarium-backup are PRESERVED.
#                        100% reversible — run ./scripts/up.sh to bring it back.
#
# --data                 also delete ~/vivarium-home (your code + agent auth)
# --backups              also delete ~/vivarium-backup (snapshot history + logs)
# --everything           all of the above + delete the ~/vivarium repo itself
# --yes / -y             skip confirmation prompts
# --dry-run / -n         print what would be done; make no changes
# --help / -h            this message

set -euo pipefail

REMOVE_DATA=false
REMOVE_BACKUPS=false
REMOVE_REPO=false
SKIP_PROMPT=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data)       REMOVE_DATA=true ;;
    --backups)    REMOVE_BACKUPS=true ;;
    --everything) REMOVE_DATA=true; REMOVE_BACKUPS=true; REMOVE_REPO=true ;;
    --yes|-y)     SKIP_PROMPT=true ;;
    --dry-run|-n) DRY_RUN=true ;;
    -h|--help)
      awk '/^# /{sub(/^# ?/,""); print; next} /^[^#]/{exit}' "$0"
      exit 0
      ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
  shift
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIVARIUM_DIR="$(dirname "$HERE")"
VIVARIUM_HOME="${VIVARIUM_HOME:-$HOME/vivarium-home}"
VIVARIUM_BACKUP="${VIVARIUM_BACKUP:-$HOME/vivarium-backup}"

yep() { if $DRY_RUN; then echo "  [dry-run] $*"; else echo "  [run] $*"; eval "$@"; fi; }

echo "vivarium remove plan:"
echo "  - stop + remove container 'vivarium' (if running)"
echo "  - remove docker image vivarium:latest (if present)"
echo "  - remove vivarium cron entries"
echo "  - remove vivarium tailnet forwarder systemd unit"
$REMOVE_DATA    && echo "  - DELETE $VIVARIUM_HOME (your work dir + agent auth)"
$REMOVE_BACKUPS && echo "  - DELETE $VIVARIUM_BACKUP (snapshot history + log files)"
$REMOVE_REPO    && echo "  - DELETE $VIVARIUM_DIR (the vivarium repo itself)"
echo ""

if ! $SKIP_PROMPT && ! $DRY_RUN; then
  read -p "proceed? [y/N] " reply
  case "$reply" in [yY]|[yY][eE][sS]) ;; *) echo "aborted."; exit 0 ;; esac
fi

cd "$VIVARIUM_DIR"

# container
if docker ps -a --format '{{.Names}}' | grep -qx vivarium; then
  yep "docker compose down --remove-orphans"
else
  echo "  [skip] no vivarium container"
fi

# image
if docker image inspect vivarium:latest >/dev/null 2>&1; then
  yep "docker rmi vivarium:latest"
else
  echo "  [skip] no vivarium:latest image"
fi

# cron
if [ -x "$VIVARIUM_DIR/scripts/cron-uninstall.sh" ]; then
  yep "bash '$VIVARIUM_DIR/scripts/cron-uninstall.sh'"
fi

# tailnet forwarder
if [ -x "$VIVARIUM_DIR/scripts/forwarder-uninstall.sh" ]; then
  yep "bash '$VIVARIUM_DIR/scripts/forwarder-uninstall.sh'"
fi

# optional: work dir + auth
if $REMOVE_DATA; then
  if [ -d "$VIVARIUM_HOME" ]; then
    yep "rm -rf '$VIVARIUM_HOME'"
  else
    echo "  [skip] $VIVARIUM_HOME does not exist"
  fi
fi

# optional: backups + logs
if $REMOVE_BACKUPS; then
  if [ -d "$VIVARIUM_BACKUP" ]; then
    yep "rm -rf '$VIVARIUM_BACKUP'"
  else
    echo "  [skip] $VIVARIUM_BACKUP does not exist"
  fi
  yep "rm -f '$HOME/vivarium-backup.log' '$HOME/vivarium-audit.log'"
fi

# optional: the repo itself (must cd out first)
if $REMOVE_REPO; then
  if [ -d "$VIVARIUM_DIR" ]; then
    cd "$HOME"
    yep "rm -rf '$VIVARIUM_DIR'"
  else
    echo "  [skip] $VIVARIUM_DIR does not exist"
  fi
fi

echo ""
echo "[remove] done."
$DRY_RUN && echo "(dry run — nothing was actually changed)"
