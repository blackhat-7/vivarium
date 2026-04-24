#!/usr/bin/env bash
# vivarium user setup — run once as the keeper user. Idempotent.

set -euo pipefail

VIVARIUM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { echo "[setup-user] $*"; }
die() { echo "[setup-user][FATAL] $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] || die "must run as keeper, not root"
[[ $(whoami) == "keeper" ]] || log "warning: not running as 'keeper' (you are $(whoami))"

log "creating directories"
mkdir -p ~/work ~/backup ~/.claude ~/.config/opencode ~/.cache
install -m 700 -d ~/.secrets

log "installing Claude Code"
if ! command -v claude &>/dev/null; then
  curl -sSL https://code.claude.com/install.sh | bash
  # install.sh typically puts the binary in ~/.local/bin — make sure PATH picks it up
  if ! grep -q 'local/bin' ~/.bashrc 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
  fi
  export PATH="$HOME/.local/bin:$PATH"
else
  log "claude already installed: $(claude --version 2>&1 | head -1)"
fi

log "installing opencode"
if ! command -v opencode &>/dev/null; then
  curl -fsSL https://opencode.ai/install | bash || log "opencode install failed — you can retry later"
else
  log "opencode already installed: $(opencode --version 2>&1 | head -1)"
fi

log "installing configs"
cp "$VIVARIUM_DIR/configs/claude-settings.json" ~/.claude/settings.json
cp "$VIVARIUM_DIR/configs/opencode.json" ~/.config/opencode/opencode.json
cp "$VIVARIUM_DIR/configs/gitignore-global" ~/.gitignore_global

log "configuring git"
git config --global core.hooksPath /dev/null
git config --global core.excludesfile ~/.gitignore_global
git config --global credential.helper store
git config --global init.defaultBranch main
git config --global pull.rebase false
# placeholder identity — set real values when you clone your first repo
git config --global user.name "keeper" 2>/dev/null || true
git config --global user.email "keeper@vivarium.local" 2>/dev/null || true

log "setting npm to ignore install scripts by default"
# install nvm + latest LTS node so npm exists under keeper
if [[ ! -d ~/.nvm ]]; then
  log "installing nvm + node LTS (this takes ~1 min)"
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
  # shellcheck disable=SC1091
  export NVM_DIR="$HOME/.nvm"
  [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
  nvm install --lts
fi
# shellcheck disable=SC1091
[ -s "$HOME/.nvm/nvm.sh" ] && . "$HOME/.nvm/nvm.sh"
npm config set ignore-scripts true

log "installing uv (fast python package manager)"
if ! command -v uv &>/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

log "installing cron jobs (backup every 2h, audit monthly)"
BACKUP_CRON="0 */2 * * * bash $HOME/vivarium/scripts/backup.sh >> $HOME/backup.log 2>&1"
AUDIT_CRON="0 9 1 * * bash $HOME/vivarium/scripts/audit.sh > $HOME/audit.log 2>&1"

# replace existing vivarium cron entries, keep others
CURRENT_CRON="$(crontab -l 2>/dev/null | grep -v 'vivarium/scripts' || true)"
{
  echo "$CURRENT_CRON"
  echo "$BACKUP_CRON"
  echo "$AUDIT_CRON"
} | crontab -

log "running an initial backup"
bash "$VIVARIUM_DIR/scripts/backup.sh" || log "backup failed (probably ~/work is empty — fine)"

log "done."
cat <<'EOF'

Next steps:

  1. Generate a fine-grained GitHub PAT (see PLAN.md §3.3). DO NOT skip this.
  2. Clone your first repo under ~/work:
       cd ~/work
       git clone https://github.com/YOU/some-repo.git
     Paste the PAT when prompted.
  3. Set the VM's Anthropic API key:
       claude  # first run prompts for auth
  4. Walk through CHECKLIST.md top to bottom.
  5. Start a session:
       tmux new -s first
       cd ~/work/some-repo
       claude --dangerously-skip-permissions

EOF
