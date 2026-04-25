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

exec "$@"
