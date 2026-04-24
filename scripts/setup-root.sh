#!/usr/bin/env bash
# vivarium root setup — run once as root on a fresh Hetzner VM.
# Idempotent where possible.

set -euo pipefail

KEEPER_USER="keeper"
VIVARIUM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { echo "[setup-root] $*"; }
die() { echo "[setup-root][FATAL] $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root"

log "creating user $KEEPER_USER (no sudo, disabled password)"
if ! id "$KEEPER_USER" &>/dev/null; then
  adduser --disabled-password --gecos "" "$KEEPER_USER"
else
  log "user already exists; skipping creation"
fi

# ensure keeper is NOT in sudo/wheel/admin groups — defensive
for grp in sudo wheel admin; do
  if id -nG "$KEEPER_USER" | tr ' ' '\n' | grep -qx "$grp"; then
    log "removing $KEEPER_USER from $grp"
    gpasswd -d "$KEEPER_USER" "$grp" || true
  fi
done

log "installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  bubblewrap socat \
  tmux tmate \
  git curl wget ca-certificates \
  jq ripgrep fd-find sqlite3 ffmpeg \
  build-essential \
  iptables iptables-persistent netfilter-persistent \
  gnupg2 pass \
  cron

# debian's fd-find is `fdfind`; give keeper a friendlier alias later via setup-user
log "packages installed"

log "adding iptables rule: block outbound SSH (port 22) from UID $KEEPER_USER"
KEEPER_UID=$(id -u "$KEEPER_USER")

# idempotent insert — check first
if ! iptables -C OUTPUT -m owner --uid-owner "$KEEPER_UID" -p tcp --dport 22 -j REJECT 2>/dev/null; then
  iptables -A OUTPUT -m owner --uid-owner "$KEEPER_UID" -p tcp --dport 22 -j REJECT
  log "rule added"
else
  log "rule already present; skipping"
fi

# persist
mkdir -p /etc/iptables
iptables-save > /etc/iptables/rules.v4
systemctl enable netfilter-persistent
systemctl restart netfilter-persistent

log "copying vivarium dir to /home/$KEEPER_USER/vivarium for keeper to run setup-user.sh"
cp -r "$VIVARIUM_DIR" "/home/$KEEPER_USER/vivarium"
chown -R "$KEEPER_USER:$KEEPER_USER" "/home/$KEEPER_USER/vivarium"

log "done."
cat <<EOF

Next steps:

  sudo -iu $KEEPER_USER
  cd ~/vivarium
  bash scripts/setup-user.sh

EOF
