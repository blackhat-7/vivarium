#!/bin/bash
# vivarium container entrypoint — bootstraps the user home from /opt/vivarium/skel
# the first time the container sees an empty (freshly bind-mounted) /home/vivarium.

set -e

if [ ! -f "$HOME/.bashrc" ]; then
  cp -rn /opt/vivarium/skel/. "$HOME/" 2>/dev/null || true
  git config --global core.hooksPath /dev/null
  git config --global credential.helper store
  git config --global init.defaultBranch main
  git config --global pull.rebase false
fi

mkdir -p "$HOME/work" 2>/dev/null || true

# warn early if bind-mount ownership is wrong, so users don't hit
# permission-denied on first clone.
if [ ! -w "$HOME/work" ]; then
  cat >&2 <<EOF
[entrypoint] WARNING: $HOME/work is not writable by uid=$(id -u).
  fix on the host (one-time):
      sudo chown -R \$(id -u):\$(id -g) ~/vivarium-home
EOF
fi

exec "$@"
