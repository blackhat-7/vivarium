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

mkdir -p "$HOME/work"

exec "$@"
