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
  if git -C "$repo" ls-files 2>/dev/null \
       | grep -qE '\.(env|pem|key|p12)$|^\.secrets/|(^|/)\.git-credentials$|(^|/)id_rsa($|\.)'; then
    fail "$repo has a tracked secret-like file"
    TRACKED_SECRETS=$((TRACKED_SECRETS+1))
  fi
done < <(find "$WORK" -maxdepth 3 -type d -name .git -print0 2>/dev/null)
[[ $REPOS -eq 0 ]] && warn "no repos under $WORK"
[[ $REPOS -gt 0 && $BAD_REMOTES -eq 0 ]] && pass "$REPOS repos, all https origins"
[[ $REPOS -gt 0 && $TRACKED_SECRETS -eq 0 ]] && pass "no tracked secret-like files"

# Per-repo git config can override the global core.hooksPath=/dev/null
# defense or wire a script to run on every git op. All five keys below
# have legitimate uses but warrant a manual review on the audit pass.
hdr "Per-repo git config (persistence vectors)"
DRIFT=0
DANGEROUS_KEYS=(core.fsmonitor core.editor core.pager core.sshCommand core.hooksPath)
while IFS= read -r -d '' gitdir; do
  repo="$(dirname "$gitdir")"
  for key in "${DANGEROUS_KEYS[@]}"; do
    val=$(git -C "$repo" config --local --get "$key" 2>/dev/null || true)
    if [[ -n "$val" ]]; then
      warn "$repo has $key=$val (runs on git ops; review)"
      DRIFT=$((DRIFT+1))
    fi
  done
done < <(find "$WORK" -maxdepth 3 -type d -name .git -print0 2>/dev/null)
[[ $DRIFT -eq 0 && $REPOS -gt 0 ]] && pass "no per-repo dangerous git config"

# MCP entries re-launch a configured command on every claude/opencode
# session. A foreign entry is a textbook persistence vector: invisible
# in normal use, runs with full agent permissions next session.
hdr "MCP config drift"
if ! command -v jq >/dev/null 2>&1; then
  warn "jq not installed on host; skipping MCP drift check (apt install jq)"
else
  KNOWN_MCP=(bestiary)
  mcp_check() {
    local file="$1" jq_path="$2" name="$3"
    [[ -f "$file" ]] || return 0
    local foreign=0 entry known
    while IFS= read -r entry; do
      [[ -z "$entry" ]] && continue
      known=0
      for k in "${KNOWN_MCP[@]}"; do
        [[ "$entry" == "$k" ]] && known=1
      done
      if [[ $known -eq 0 ]]; then
        fail "$name MCP entry '$entry' not in known-good list — possible agent persistence"
        foreign=$((foreign+1))
      fi
    done < <(jq -r "($jq_path // {}) | keys[]?" "$file" 2>/dev/null)
    [[ $foreign -eq 0 ]] && pass "$name MCP entries are known-good"
  }
  mcp_check "$VIVARIUM_HOME/.config/opencode/opencode.json" '.mcp' "opencode"
  mcp_check "$VIVARIUM_HOME/.claude.json" '.mcpServers' "claude"
fi

# ssh client is not in the image, so any keys in ~/.ssh imply the user
# wired an out-of-band push path — bypassing the read-only PAT defense.
hdr "SSH (ssh client is not in the image)"
if [[ -d "$VIVARIUM_HOME/.ssh" ]]; then
  ssh_count=$(find "$VIVARIUM_HOME/.ssh" -type f 2>/dev/null | wc -l)
  if [[ $ssh_count -gt 0 ]]; then
    warn "$VIVARIUM_HOME/.ssh has $ssh_count file(s); ssh client is absent but keys here imply an out-of-band push path"
  else
    pass "$VIVARIUM_HOME/.ssh exists but is empty"
  fi
else
  pass "no .ssh directory in vivarium-home"
fi

hdr "Reminders (manual)"
echo "  - Rotate GitHub PAT every 30 days (https://github.com/settings/tokens)"
echo "  - Rotate provider OAuth tokens monthly (re-run: opencode auth login / claude login)"
echo "  - If using pay-per-token providers: verify monthly cap in provider console"
echo "  - Skim \`docker logs vivarium\` for anything weird"
echo "  - Quarterly: \`docker compose pull && docker compose up -d --build\` to refresh base image"

hdr "Summary"
echo "  failures: $FAIL"
echo "  warnings: $WARN"

[[ $FAIL -eq 0 ]] || exit 1
