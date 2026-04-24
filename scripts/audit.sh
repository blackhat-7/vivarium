#!/usr/bin/env bash
# vivarium monthly drift audit — run on the 1st of each month.
# Prints a report; exits non-zero if any critical drift is found.

set -uo pipefail   # NOT -e: we want to report all failures

FAIL=0
WARN=0

pass() { echo "  [OK]   $*"; }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
warn() { echo "  [WARN] $*"; WARN=$((WARN+1)); }
hdr()  { echo; echo "## $*"; }

echo "=== vivarium audit — $(date -Iseconds) ==="

hdr "Supply-chain guards"
if [[ "$(npm config get ignore-scripts 2>/dev/null)" == "true" ]]; then
  pass "npm ignore-scripts = true"
else
  fail "npm ignore-scripts is NOT true — run: npm config set ignore-scripts true"
fi

HOOKS_PATH=$(git config --global --get core.hooksPath 2>/dev/null || echo "")
if [[ "$HOOKS_PATH" == "/dev/null" ]]; then
  pass "git core.hooksPath = /dev/null"
else
  fail "git core.hooksPath is '$HOOKS_PATH' — run: git config --global core.hooksPath /dev/null"
fi

EXCL=$(git config --global --get core.excludesfile 2>/dev/null || echo "")
if [[ -n "$EXCL" ]] && [[ -f "$EXCL" ]] && grep -q '\.env' "$EXCL"; then
  pass "global gitignore is active and ignores .env"
else
  fail "global gitignore missing or does not ignore .env"
fi

hdr "Network fence"
KEEPER_UID=$(id -u)
if sudo -n iptables -L OUTPUT -v 2>/dev/null | grep -q "owner UID match $KEEPER_UID"; then
  pass "iptables SSH block for UID $KEEPER_UID is active"
else
  warn "cannot verify iptables rule as non-root (expected — this runs as keeper)"
  warn "verify manually by running, as root: iptables -L OUTPUT -v | grep owner"
fi

hdr "Claude sandbox config"
SETTINGS="$HOME/.claude/settings.json"
if [[ -f "$SETTINGS" ]]; then
  if jq -e '.sandbox.enabled == true' "$SETTINGS" >/dev/null; then
    pass "sandbox.enabled = true"
  else
    fail "sandbox.enabled is NOT true in $SETTINGS"
  fi
  if jq -e '.sandbox.failIfUnavailable == true' "$SETTINGS" >/dev/null; then
    pass "sandbox.failIfUnavailable = true"
  else
    warn "sandbox.failIfUnavailable is not true — Claude could run unsandboxed if bwrap breaks"
  fi
  if jq -e '.permissions.deny | index("Bash(git push*)")' "$SETTINGS" >/dev/null; then
    pass "git push is in deny list"
  else
    fail "git push is NOT in deny list"
  fi
else
  fail "$SETTINGS missing"
fi

hdr "Backups"
LATEST=$(ls -t "$HOME/backup/" 2>/dev/null | head -1 || echo "")
if [[ -n "$LATEST" ]]; then
  AGE=$(( ($(date +%s) - $(stat -c %Y "$HOME/backup/$LATEST")) / 3600 ))
  if [[ $AGE -lt 4 ]]; then
    pass "latest backup ($LATEST) is ${AGE}h old"
  else
    fail "latest backup is ${AGE}h old — cron may not be running. check: crontab -l"
  fi
else
  fail "no backups found in ~/backup/"
fi

hdr "Repo hygiene"
REPO_COUNT=0
BAD_REMOTES=0
TRACKED_SECRETS=0
while IFS= read -r -d '' gitdir; do
  REPO_COUNT=$((REPO_COUNT+1))
  repo="$(dirname "$gitdir")"
  url=$(git -C "$repo" remote get-url origin 2>/dev/null || echo "")
  if [[ -n "$url" ]] && [[ "$url" != https://* ]]; then
    warn "  $repo has non-https origin: $url"
    BAD_REMOTES=$((BAD_REMOTES+1))
  fi
  # check for tracked secret-like files
  if git -C "$repo" ls-files 2>/dev/null | grep -qE '\.(env|pem|key|p12)$|^\.secrets/'; then
    fail "  $repo has a tracked secret-like file"
    TRACKED_SECRETS=$((TRACKED_SECRETS+1))
  fi
done < <(find "$HOME/work" -maxdepth 3 -type d -name .git -print0 2>/dev/null)

if [[ $REPO_COUNT -eq 0 ]]; then
  warn "no repos found in ~/work"
else
  [[ $BAD_REMOTES -eq 0 ]] && pass "$REPO_COUNT repos, all using https origin"
  [[ $TRACKED_SECRETS -eq 0 ]] && pass "no tracked secret-like files across $REPO_COUNT repos"
fi

hdr "Reminders (manual actions)"
echo "  - Rotate GitHub PAT if older than 90 days (https://github.com/settings/tokens)"
echo "  - Verify monthly budget cap in Anthropic console"
echo "  - Verify monthly budget cap in any other provider consoles (OpenRouter, OpenAI, etc.)"
echo "  - Run 'sudo apt update && sudo apt upgrade' on the VM (as root, not keeper)"
echo "  - Review ~/backup/ sizes; prune old daily-* if disk is tight"

hdr "Summary"
echo "  failures: $FAIL"
echo "  warnings: $WARN"

[[ $FAIL -eq 0 ]] || exit 1
