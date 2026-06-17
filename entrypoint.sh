#!/bin/bash
# vivarium container entrypoint — bootstraps the user home from /opt/vivarium/skel
# on first run, and re-applies safety-critical config every container start.
# Keep startup safety config idempotent; it protects against persistence vectors.

set -e

# First-run only: lay down the skel files. Re-runs preserve user edits.
if [ ! -f "$HOME/.bashrc" ]; then
  cp -rn /opt/vivarium/skel/. "$HOME/" 2>/dev/null || true
fi

# Migrate: an earlier skel prepended $HOME/.local/bin to PATH, where a
# planted ~/.local/bin/git would shadow the real /usr/bin/git for every
# subsequent shell. Rewrite to append instead — user-local installs
# (pip --user, uvx, pipx) still resolve, but system binaries win.
# Idempotent: no-op if the line is already in safe form or absent.
sed -i 's|^export PATH="\$HOME/\.local/bin:\$PATH"$|export PATH="$PATH:$HOME/.local/bin"|' "$HOME/.bashrc" 2>/dev/null || true
grep -q '\.npm-global/bin' "$HOME/.bashrc" 2>/dev/null || echo 'export PATH="$PATH:$HOME/.npm-global/bin"' >> "$HOME/.bashrc"
export PATH="$PATH:$HOME/.local/bin:$HOME/.npm-global/bin"
export PI_OFFLINE="${PI_OFFLINE:-1}"

# Always re-apply safety-critical git config. A compromised agent can
# flip these between starts to re-enable git hooks or swap in a
# credential helper that persists/exfiltrates the PAT. unset-then-set
# guards against `git config --add` shadowing ours with a second entry.
# Idempotent.
for k in core.hooksPath credential.helper init.defaultBranch pull.rebase; do
  git config --global --unset-all "$k" 2>/dev/null || true
done
git config --global core.hooksPath /dev/null
git config --global credential.helper 'cache --timeout=86400'
git config --global init.defaultBranch main
git config --global pull.rebase false

# Migrate from the old `store` helper: any plaintext PAT at
# ~/.git-credentials would otherwise stay agent-readable forever even
# after switching helpers, and would be copied into every backup.
[ -f "$HOME/.git-credentials" ] && rm -f "$HOME/.git-credentials"

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

# Re-apply AI harness configs generated at image build time. These files are
# intentionally overwritten on every start so an agent cannot persistently
# enable MCPs or weaken/alter harness permissions by editing its home dir.
apply_ai_harnesses() {
  local src=/opt/vivarium/ai-harnesses-home rel
  [ -d "$src" ] || return 0

  while IFS= read -r rel; do
    [ -e "$src/$rel" ] || continue
    mkdir -p "$(dirname "$HOME/$rel")"
    rm -rf "$HOME/$rel"
    cp -P "$src/$rel" "$HOME/$rel"
  done <<'EOF'
.claude/settings.json
.claude/statusline-command.sh
.claude/notify.sh
.claude.json
.config/opencode/opencode.json
.config/opencode/package.json
.config/opencode/plugins/readonly-bash.js
.config/opencode/agent/reviewer.md
.pi/agent/settings.json
.pi/agent/readonly-bash.json
.pi/agent/extensions/pi-permission-system/config.json
.pi/agent/subagents.json
.pi/agent/keybindings.json
.pi/web-search.json
.config/mcp/mcp.json
.config/mcp/mcp.catalog.json
EOF

  if [ ! -x "$HOME/.npm-global/bin/pi" ] && [ -d "$src/.npm-global" ]; then
    mkdir -p "$HOME/.npm-global"
    cp -RP "$src/.npm-global/." "$HOME/.npm-global/"
  fi
}
apply_ai_harnesses

# Re-apply global agent rules where each harness actually reads them.
install -D -m 0644 /opt/vivarium/skel/AGENTS.md "$HOME/.pi/agent/AGENTS.md"
install -D -m 0644 /opt/vivarium/skel/AGENTS.md "$HOME/.config/opencode/AGENTS.md"
install -D -m 0644 /opt/vivarium/skel/AGENTS.md "$HOME/.claude/CLAUDE.md"

# Optional remote-access mode: paseo daemon on :6767. Pairs with
# desktop/mobile/web/CLI clients via QR code shown on stdout (visible in
# `docker compose logs vivarium`). Pairing crypto is the auth.
#
# Flags:
#   --foreground   keep the daemon as PID 1 (default `daemon start` forks
#                  and exits, which crashes the container in a restart loop)
#   --no-relay     don't dial paseo.sh's hosted relay; tailscale-only by
#                  default, matches PLAN's "minimum-moving-parts" stance
#
# PASEO_HOSTNAMES is paseo's Host-header allowlist. Default upstream is
# "localhost,.localhost" — too restrictive for tailnet clients hitting the
# daemon by IP or magic-DNS hostname. We default to "true" (any host)
# because the actual auth surfaces are (a) the docker port binding
# (PASEO_BIND_ADDR — set to a tailscale IP for tailnet-only) and (b) the
# QR-paired NaCl box keys. Set PASEO_HOSTNAMES explicitly in .env to
# tighten this if you want defense-in-depth.
if [ "${PASEO_ENABLE:-}" = "true" ] && command -v paseo >/dev/null 2>&1; then
  export PASEO_LISTEN="${PASEO_LISTEN:-0.0.0.0:6767}"
  export PASEO_HOSTNAMES="${PASEO_HOSTNAMES:-true}"
  # Stale pidfile cleanup: paseo writes ~/.paseo/paseo.pid with the daemon's
  # PID and refuses to start if it sees one. After a `docker compose down/up`
  # the file persists in the bind-mounted home, but the PID belongs to a dead
  # container's namespace. Safe to delete unconditionally at entrypoint time:
  # by definition no paseo is running in this container yet.
  rm -f "$HOME/.paseo/paseo.pid"
  echo "[entrypoint] PASEO_ENABLE=true — starting paseo daemon on ${PASEO_LISTEN}"
  echo "[entrypoint]   hostnames allowlist: ${PASEO_HOSTNAMES}"
  echo "[entrypoint]   pair from your phone: open paseo, scan the QR code printed below"
  exec paseo daemon start --foreground --no-relay
fi

exec "$@"
