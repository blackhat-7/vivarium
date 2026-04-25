#!/usr/bin/env bash
# install the host-side socat forwarder that bridges tailscale traffic to the
# vivarium opencode listener.
#
# why this exists: Docker Desktop on Linux runs the daemon in a VM, so its
# port-forwarder cannot bind to host-specific IPs like the tailscale interface
# (100.64.0.1). The container binds to 127.0.0.1 instead, and this systemd
# user unit relays 100.64.0.1:4096 -> 127.0.0.1:4096 from the host. With a
# rootful Docker daemon the relay isn't needed (set OPENCODE_FORWARD_ADDR
# equal to OPENCODE_BIND_ADDR in .env, or just don't run this script).
#
# idempotent — re-running rewrites the unit and restarts.
# uninstalls itself when the bind addr equals the forward addr (no-op needed).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIVARIUM_DIR="$(dirname "$HERE")"

# pull values from .env if present
if [ -f "$VIVARIUM_DIR/.env" ]; then
  set -a; . "$VIVARIUM_DIR/.env"; set +a
fi

BIND_ADDR="${OPENCODE_BIND_ADDR:-127.0.0.1}"
FORWARD_ADDR="${OPENCODE_FORWARD_ADDR:-100.64.0.1}"
PORT="${OPENCODE_PORT:-4096}"

UNIT_NAME="vivarium-tailnet-forward.service"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

# nothing to do if the container is already bound to the tailscale interface
if [ "$BIND_ADDR" = "$FORWARD_ADDR" ]; then
  echo "[forwarder-install] OPENCODE_BIND_ADDR == OPENCODE_FORWARD_ADDR ($BIND_ADDR);"
  echo "                    container binds tailscale directly — forwarder not needed."
  exec bash "$HERE/forwarder-uninstall.sh"
fi

if ! command -v socat >/dev/null 2>&1; then
  echo "[forwarder-install] FATAL: socat not found on host." >&2
  echo "                    install it (Arch: 'sudo pacman -S socat') and re-run." >&2
  exit 1
fi
SOCAT_BIN="$(command -v socat)"

mkdir -p "$UNIT_DIR"
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=vivarium tailnet -> loopback forwarder ($FORWARD_ADDR:$PORT -> $BIND_ADDR:$PORT)
Documentation=file://$VIVARIUM_DIR/scripts/forwarder-install.sh
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$SOCAT_BIN -d -lh TCP-LISTEN:$PORT,bind=$FORWARD_ADDR,fork,reuseaddr TCP:$BIND_ADDR:$PORT
Restart=on-failure
RestartSec=2
# socat exits 1 when the bind IP isn't yet available (e.g. tailscale not up);
# RestartSec keeps it cheaply retrying until it can bind.

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

echo "[forwarder-install] active: $FORWARD_ADDR:$PORT -> $BIND_ADDR:$PORT"
systemctl --user --no-pager status "$UNIT_NAME" 2>&1 | head -8 | sed 's/^/  /'

# linger hint — without it, the unit dies when the user logs out
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
  cat <<EOF

[forwarder-install] NOTE: linger is not enabled for $USER. The forwarder will
  stop when you log out of all sessions. To keep it running across logouts:
    sudo loginctl enable-linger $USER
EOF
fi
