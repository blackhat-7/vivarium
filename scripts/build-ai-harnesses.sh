#!/usr/bin/env bash
set -euo pipefail

ref="${1:-main}"
mcp="${2:-none}"
arch="${TARGETARCH:-amd64}"

case "$arch" in
  amd64) system=x86_64-linux ;;
  arm64) system=aarch64-linux ;;
  *) echo "[FATAL] unsupported Docker TARGETARCH: $arch" >&2; exit 1 ;;
esac

mcp_servers=null
case "${mcp//[[:space:]]/}" in
  ""|none|false) mcp_enable=false ;;
  all|true) mcp_enable=true ;;
  *)
    mcp_enable=true
    IFS=, read -ra names <<< "${mcp//[[:space:]]/}"
    mcp_servers="["
    for name in "${names[@]}"; do
      [[ "$name" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "[FATAL] invalid MCP name: $name" >&2; exit 1; }
      mcp_servers+=" \"$name\""
    done
    mcp_servers+=" ]"
    ;;
esac

if ! command -v nix >/dev/null 2>&1; then
  mkdir -p /nix
  curl -fsSL https://nixos.org/nix/install | sh -s -- --no-daemon
fi
export PATH=/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:$PATH
mkdir -p /etc/nix /opt/vivarium/ai-harnesses-profile /opt/vivarium/ai-harnesses-home
printf '%s\n' 'experimental-features = nix-command flakes' > /etc/nix/nix.conf

cat > /opt/vivarium/ai-harnesses-profile/flake.nix <<EOF
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    home-manager.url = "github:nix-community/home-manager";
    home-manager.inputs.nixpkgs.follows = "nixpkgs";
    ai-harnesses.url = "github:blackhat-7/ai-harnesses/${ref}";
  };

  outputs = { nixpkgs, home-manager, ai-harnesses, ... }: {
    homeConfigurations.vivarium = home-manager.lib.homeManagerConfiguration {
      pkgs = import nixpkgs { system = "${system}"; };
      modules = [
        ai-harnesses.homeManagerModules.default
        {
          home.username = "root";
          home.homeDirectory = "/opt/vivarium/ai-harnesses-home";
          home.stateVersion = "24.11";
          aiHarnesses.mode = "yolo";
          aiHarnesses.mcp.enable = ${mcp_enable};
          aiHarnesses.mcp.enabledServers = ${mcp_servers};
        }
      ];
    };
  };
}
EOF

nix build /opt/vivarium/ai-harnesses-profile#homeConfigurations.vivarium.activationPackage \
  -o /opt/vivarium/ai-harnesses-activation

HOME=/opt/vivarium/ai-harnesses-home USER=root \
  /opt/vivarium/ai-harnesses-activation/activate
