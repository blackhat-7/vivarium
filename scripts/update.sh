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
profile_arg="${1:-}"
if [[ $# -gt 1 ]]; then
  echo "usage: $0 [profile-name|env-file]" >&2
  exit 1
fi
# shellcheck disable=SC1091
. ./scripts/profile.sh "$profile_arg"

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

resolve_ref_for_build() {
  local env_key="$1" repo="$2" label="$3" ref sha
  ref="${!env_key:-}"
  if [ -n "$ref" ] && [[ ! "$ref" =~ ^[0-9a-f]{40}$ ]]; then
    sha=$(git ls-remote "$repo" "$ref" 2>/dev/null | head -1 | cut -f1)
    if [ -n "$sha" ]; then
      echo "[update] $label $ref -> ${sha:0:12}"
      export "$env_key=$sha"
    else
      echo "[update] WARNING: could not resolve $label ref '$ref' — container may use a stale cached layer." >&2
    fi
  fi
}

# cache-bust moving refs before docker compose build.
if [[ "${INSTALL_BESTIARY:-}" == true ]]; then
  resolve_ref_for_build BESTIARY_REF https://github.com/blackhat-7/bestiary.git bestiary
fi
resolve_ref_for_build AI_HARNESSES_REF https://github.com/blackhat-7/ai-harnesses.git ai-harnesses

echo
echo "[update] rebuilding (cache-hit layers stay; only changed layers rebuild)"
echo "[update]   $VIVARIUM_HOME survives (bind mount): auth, code, configs"
echo "[update]   in-container processes will be killed: opencode, tmux"
echo

"$(dirname "${BASH_SOURCE[0]}")/up.sh" ${profile_arg:+"$profile_arg"}
