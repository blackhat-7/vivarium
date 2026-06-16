#!/usr/bin/env bash
# Prime the running vivarium container with a GitHub HTTPS read credential.
# git uses it directly; gh reads the same cache through /usr/local/bin/gh.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
profile_arg="${1:-}"
if [[ $# -gt 1 ]]; then
  echo "usage: $0 [profile-name|env-file]" >&2
  exit 1
fi
# shellcheck disable=SC1091
. ./scripts/profile.sh "$profile_arg"

# Keep the token as a host-local shell variable. Do not pass it as a normal
# container environment variable and do not write it to ~/.git-credentials.
github_read_token="${GITHUB_READ_TOKEN:-}"
export -n GITHUB_READ_TOKEN 2>/dev/null || true
unset GITHUB_READ_TOKEN

if [[ -z "$github_read_token" ]]; then
  echo "[git-auth] GITHUB_READ_TOKEN is not set in $VIVARIUM_ENV_FILE; skipping"
  exit 0
fi

if ! vivarium_compose ps --status running --services 2>/dev/null | grep -qx vivarium; then
  echo "[git-auth] $CONTAINER_NAME is not running. start it with: ./scripts/up.sh${profile_arg:+ $profile_arg}" >&2
  exit 1
fi

echo "[git-auth] priming GitHub HTTPS read credential for normal git clone"
# Re-apply the safe cache helper immediately before approving the credential,
# so a changed helper cannot persist the token to disk.
printf 'protocol=https\nhost=github.com\nusername=x-access-token\npassword=%s\n\n' \
  "$github_read_token" \
  | vivarium_compose exec -T vivarium bash -lc '
      set -euo pipefail
      git config --global --unset-all credential.helper 2>/dev/null || true
      git config --global credential.helper "cache --timeout=86400"
      rm -f "$HOME/.git-credentials"
      git -c credential.helper= -c credential.helper="cache --timeout=86400" credential approve
    ' >/dev/null
unset github_read_token

echo "[git-auth] done (git and gh use this cached credential)"
