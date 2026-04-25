#!/bin/bash
# vivarium container entrypoint — bootstraps the user home from /opt/vivarium/skel
# on first run, and re-applies safety-critical config every container start.
# See PLAN.md §6.2 (hooks) and §6.7 (persistence vectors).

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

# Auto-wire optional MCP servers into agent configs. Only ADDS missing
# entries — never overwrites or removes user customizations. Runs every
# container start; idempotent. Triggered by the binary's presence in the
# image (i.e. the corresponding INSTALL_* flag was true at build time).
wire_mcp_entry() {
  local cfg="$1" check_expr="$2" merge_expr="$3" name="$4"
  mkdir -p "$(dirname "$cfg")"
  [ -f "$cfg" ] || echo '{}' > "$cfg"
  # if entry exists, do nothing (preserves user customizations)
  if jq -e "$check_expr" "$cfg" >/dev/null 2>&1; then
    return 0
  fi
  local tmp
  tmp=$(mktemp)
  if jq "$merge_expr" "$cfg" > "$tmp" 2>/dev/null; then
    mv "$tmp" "$cfg"
    echo "[entrypoint] wired $name into $cfg"
  else
    rm -f "$tmp"
    echo "[entrypoint] WARNING: could not merge $name into $cfg (invalid JSON?)" >&2
  fi
}

if command -v bestiary >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
  wire_mcp_entry \
    "$HOME/.config/opencode/opencode.json" \
    '.mcp.bestiary' \
    '.mcp.bestiary = {type:"local", command:["bestiary","serve"], enabled:true}' \
    "bestiary MCP"
  wire_mcp_entry \
    "$HOME/.claude.json" \
    '.mcpServers.bestiary' \
    '.mcpServers.bestiary = {command:"bestiary", args:["serve"]}' \
    "bestiary MCP"
fi

# Optional remote-access mode — at most one listener at a time.
# Paseo wins over opencode-web if both are set: it's a multi-agent
# (claude/codex/opencode) superset that pairs with desktop/mobile/web/CLI
# clients via QR code shown on stdout (visible in `docker compose logs
# vivarium`). Pairing crypto is the auth — no password env needed.
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
  echo "[entrypoint] PASEO_ENABLE=true — starting paseo daemon on ${PASEO_LISTEN}"
  echo "[entrypoint]   hostnames allowlist: ${PASEO_HOSTNAMES}"
  echo "[entrypoint]   pair from your phone: open paseo, scan the QR code printed below"
  exec paseo daemon start --foreground --no-relay
fi

# Opencode-web: HTTP Basic (username "opencode", password from env).
if [ -n "${OPENCODE_SERVER_PASSWORD:-}" ] && command -v opencode >/dev/null 2>&1; then
  echo "[entrypoint] OPENCODE_SERVER_PASSWORD is set — starting opencode web on :4096"
  exec opencode web --port 4096 --hostname 0.0.0.0
fi

exec "$@"
