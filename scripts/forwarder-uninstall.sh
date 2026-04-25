#!/usr/bin/env bash
# remove the host-side socat forwarder unit.
# idempotent — safe to run when nothing is installed.

set -euo pipefail

UNIT_NAME="vivarium-tailnet-forward.service"
UNIT_PATH="$HOME/.config/systemd/user/$UNIT_NAME"

if systemctl --user list-unit-files "$UNIT_NAME" 2>/dev/null | grep -q "$UNIT_NAME"; then
  systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
fi

if [ -f "$UNIT_PATH" ]; then
  rm -f "$UNIT_PATH"
  systemctl --user daemon-reload
  echo "[forwarder-uninstall] removed $UNIT_PATH"
else
  echo "[forwarder-uninstall] no unit installed — nothing to remove"
fi
