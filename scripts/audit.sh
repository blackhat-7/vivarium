#!/usr/bin/env bash
# monthly health/drift check. runs on the host.
# install as a cron: 0 9 1 * * bash ~/vivarium/scripts/audit.sh > ~/vivarium-audit.log 2>&1

set -uo pipefail

FAIL=0; WARN=0
pass() { echo "  [OK]   $*"; }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
warn() { echo "  [WARN] $*"; WARN=$((WARN+1)); }
hdr()  { echo; echo "## $*"; }

VIVARIUM_HOME="${VIVARIUM_HOME:-$HOME/vivarium-home}"
BACKUP_ROOT="${VIVARIUM_BACKUP:-$HOME/vivarium-backup}"

echo "=== vivarium audit — $(date -Iseconds) ==="

hdr "Container"
if docker ps --format '{{.Names}}' | grep -q '^vivarium$'; then
  pass "vivarium container is running"
else
  fail "vivarium container not running — start with scripts/up.sh"
fi

hdr "Backups"
LATEST=$(ls -t "$BACKUP_ROOT/" 2>/dev/null | head -1 || echo "")
if [[ -n "$LATEST" ]]; then
  AGE=$(( ($(date +%s) - $(stat -c %Y "$BACKUP_ROOT/$LATEST")) / 3600 ))
  if [[ $AGE -lt 4 ]]; then
    pass "latest backup ($LATEST) is ${AGE}h old"
  else
    fail "latest backup is ${AGE}h old — check cron"
  fi
else
  warn "no backups in $BACKUP_ROOT"
fi

hdr "Repo hygiene (inside vivarium-home/work)"
WORK="$VIVARIUM_HOME/work"
REPOS=0; BAD_REMOTES=0; TRACKED_SECRETS=0
while IFS= read -r -d '' gitdir; do
  REPOS=$((REPOS+1))
  repo="$(dirname "$gitdir")"
  url=$(git -C "$repo" remote get-url origin 2>/dev/null || echo "")
  if [[ -n "$url" ]] && [[ "$url" != https://* ]]; then
    warn "$repo has non-https origin: $url"
    BAD_REMOTES=$((BAD_REMOTES+1))
  fi
  if git -C "$repo" ls-files 2>/dev/null | grep -qE '\.(env|pem|key|p12)$|^\.secrets/'; then
    fail "$repo has a tracked secret-like file"
    TRACKED_SECRETS=$((TRACKED_SECRETS+1))
  fi
done < <(find "$WORK" -maxdepth 3 -type d -name .git -print0 2>/dev/null)
[[ $REPOS -eq 0 ]] && warn "no repos under $WORK"
[[ $REPOS -gt 0 && $BAD_REMOTES -eq 0 ]] && pass "$REPOS repos, all https origins"
[[ $REPOS -gt 0 && $TRACKED_SECRETS -eq 0 ]] && pass "no tracked secret-like files"

hdr "Reminders (manual)"
echo "  - Rotate GitHub PAT if appropriate (https://github.com/settings/tokens)"
echo "  - If using pay-per-token providers: verify monthly cap in provider console"
echo "  - Skim \`docker logs vivarium\` for anything weird"
echo "  - Consider \`docker compose pull && docker compose up -d --build\` to refresh base image"

hdr "Summary"
echo "  failures: $FAIL"
echo "  warnings: $WARN"

[[ $FAIL -eq 0 ]] || exit 1
