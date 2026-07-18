#!/usr/bin/env bash
# Create a new vivarium profile env file.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

name="${1:-}"
[[ -n "$name" ]] || { echo "usage: $0 <profile-name>" >&2; exit 1; }
[[ "$name" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || {
  echo "invalid profile name '$name' (use lowercase letters, numbers, _ or -)" >&2
  exit 1
}

file="profiles/$name.env"
[[ ! -e "$file" ]] || { echo "profile already exists: $file" >&2; exit 1; }
mkdir -p profiles
cat > "$file" <<EOF
COMPOSE_PROJECT_NAME=vivarium-$name
CONTAINER_NAME=vivarium-$name
VIVARIUM_HOME=$HOME/vivarium-home-$name
VIVARIUM_BACKUP=$HOME/vivarium-backup-$name

INSTALL_OPENCODE=true
INSTALL_CLAUDE=false
INSTALL_PASEO=false
PASEO_ENABLE=false
# PASEO_BIND_ADDR=127.0.0.1
# PASEO_PORT=6767
# PASEO_HOSTNAMES=
INSTALL_BESTIARY=false
BESTIARY_REF=main

AI_HARNESSES_REF=main
AI_HARNESSES_MCP=none

# Optional GitHub HTTPS clone token. scripts/up.sh loads this into git's
# credential cache without passing it as container env.
# GITHUB_READ_TOKEN=

# The optional push gate is host-wide, not profile-specific:
# ./scripts/push-gate.sh enable

# Optional MCP tokens; keep read-only / low-blast-radius.
# GITHUB_MCP_TOKEN=
# AFTERSHOOT_MCP_API_KEY=
# LINEAR_API_KEY=
EOF
chmod 600 "$file"
echo "created $file"
echo "start with: ./scripts/up.sh $name"
