#!/usr/bin/env bash
# update vivarium: pull latest from origin/main and rebuild the container
# if there were any new commits.
#
# preserved across the rebuild (they live on the host, bind-mounted in):
#   ~/vivarium-home/                — opencode/claude auth, ~/.claude.json,
#                                      cloned repos, .env.d/, shell history
#
# NOT preserved (these are in-container process state):
#   running opencode sessions, tmux sessions, in-flight git operations
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# refuse to pull on top of uncommitted changes — a silent auto-merge is
# worse than failing fast.
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "[update] working tree has uncommitted changes:" >&2
  git status -sb >&2
  echo "[update] commit, stash, or discard them before updating." >&2
  exit 1
fi

old_head=$(git rev-parse HEAD)
echo "[update] fetching origin/main"
git fetch --quiet origin main
new_head=$(git rev-parse origin/main)

if [ "$old_head" = "$new_head" ]; then
  echo "[update] vivarium already at $(git rev-parse --short HEAD)"
else
  echo "[update] new vivarium commits:"
  git log --oneline "$old_head".."$new_head" | sed 's/^/  /'
  git merge --ff-only "$new_head"
fi

# bestiary cache-busting: if BESTIARY_REF is a branch/tag (not a 40-char
# SHA), resolve it to the upstream commit SHA and override BESTIARY_REF
# for the build. without this, docker's layer cache hits on the stale
# "main" string forever and you'd never pick up bestiary commits.
if grep -qE '^INSTALL_BESTIARY=true$' .env 2>/dev/null; then
  ref=$(awk -F= '/^BESTIARY_REF=/{print $2; exit}' .env)
  if [ -n "$ref" ] && [[ ! "$ref" =~ ^[0-9a-f]{40}$ ]]; then
    sha=$(git ls-remote https://github.com/blackhat-7/bestiary.git "$ref" 2>/dev/null | head -1 | cut -f1)
    if [ -n "$sha" ]; then
      echo "[update] bestiary $ref -> ${sha:0:12}"
      export BESTIARY_REF="$sha"
    else
      echo "[update] WARNING: could not resolve bestiary ref '$ref' — container may not pick up upstream changes." >&2
    fi
  fi
fi

echo
echo "[update] rebuilding (cache-hit layers stay; only changed layers rebuild)"
echo "[update]   ~/vivarium-home/ survives (bind mount): auth, code, configs"
echo "[update]   in-container processes will be killed: opencode, tmux"
echo

"$(dirname "${BASH_SOURCE[0]}")/up.sh"
