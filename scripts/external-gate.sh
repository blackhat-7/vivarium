#!/usr/bin/env bash
# Manage the one hardened external gate shared by every Vivarium profile.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
CONFIG_DIR="$HOME/.config/vivarium"
CONFIG_FILE="$CONFIG_DIR/external-gate.env"
STATE_DIR="$HOME/.local/state/vivarium-external-gate"
SOCKET_DIR="$HOME/.local/share/vivarium-external-gate"
RUNTIME_FILE="$STATE_DIR/runtime-fingerprint"
COMPOSE_FILE="$(pwd)/compose.external-gate.yaml"
APPROVAL_PORT=7843

fatal() {
  echo "[FATAL] $* Disable with: ./scripts/external-gate.sh disable" >&2
  exit 1
}

random_password() {
  od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
}

lock_lifecycle() {
  install -d -m 0700 "$STATE_DIR"
  exec 9>"$STATE_DIR/lifecycle.lock"
  flock -x 9
}

require_commands() {
  local command
  for command in docker curl python3 ssh-add sha256sum stat; do
    command -v "$command" >/dev/null || fatal "$command is required"
  done
  docker compose version >/dev/null 2>&1 || fatal "Docker Compose v2 is required"
}

read_config() {
  [[ -f "$CONFIG_FILE" ]] || fatal "external gate is not enabled; run: ./scripts/external-gate.sh enable"
  [[ "$(stat -c '%a:%u' "$CONFIG_FILE" 2>/dev/null)" == "600:$(id -u)" ]] \
    || fatal "$CONFIG_FILE must be owned by you with mode 0600"

  EXTERNAL_GATE_ENABLE=
  EXTERNAL_GATE_APPROVAL_PASSWORD=
  EXTERNAL_GATE_APPROVAL_MODE=
  EXTERNAL_GATE_APPROVAL_BIND_ADDR=
  EXTERNAL_GATE_PUBLIC_URL=
  EXTERNAL_GATE_SSH_KEY_FINGERPRINT=
  declare -A seen=()
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == *=* ]] || fatal "invalid line in $CONFIG_FILE"
    key=${line%%=*}
    value=${line#*=}
    case "$key" in
      EXTERNAL_GATE_ENABLE|EXTERNAL_GATE_APPROVAL_PASSWORD|EXTERNAL_GATE_APPROVAL_MODE|EXTERNAL_GATE_APPROVAL_BIND_ADDR|EXTERNAL_GATE_PUBLIC_URL|EXTERNAL_GATE_SSH_KEY_FINGERPRINT) ;;
      *) fatal "unknown setting $key in $CONFIG_FILE" ;;
    esac
    [[ -z "${seen[$key]:-}" ]] || fatal "duplicate $key in $CONFIG_FILE"
    seen[$key]=1
    printf -v "$key" '%s' "$value"
  done <"$CONFIG_FILE"

  [[ ${#seen[@]} -eq 6 ]] || fatal "missing setting in $CONFIG_FILE"
  [[ "$EXTERNAL_GATE_ENABLE" == true || "$EXTERNAL_GATE_ENABLE" == false ]] || fatal "invalid EXTERNAL_GATE_ENABLE"
  [[ ${#EXTERNAL_GATE_APPROVAL_PASSWORD} -ge 20 ]] || fatal "approval password must contain at least 20 characters"
  [[ "$EXTERNAL_GATE_SSH_KEY_FINGERPRINT" == SHA256:* ]] || fatal "invalid SSH key fingerprint"
  validate_transport
}

validate_transport() {
  python3 - "$EXTERNAL_GATE_APPROVAL_MODE" "$EXTERNAL_GATE_APPROVAL_BIND_ADDR" "$EXTERNAL_GATE_PUBLIC_URL" "$APPROVAL_PORT" <<'PY' \
    || fatal "invalid approval listener/public-origin pairing"
import ipaddress
import sys
from urllib.parse import urlsplit

mode, bind, public, port_text = sys.argv[1:]
port = int(port_text)
parsed = urlsplit(public)
if (
    parsed.scheme not in {"http", "https"}
    or not parsed.hostname
    or parsed.username is not None
    or parsed.password is not None
    or parsed.path
    or parsed.query
    or parsed.fragment
    or public.endswith("/")
):
    raise SystemExit(1)
try:
    public_port = parsed.port or (443 if parsed.scheme == "https" else 80)
except ValueError:
    raise SystemExit(1)
if mode == "loopback":
    valid = bind == "127.0.0.1" and public == f"http://127.0.0.1:{port}"
elif mode == "proxy":
    valid = bind == "127.0.0.1" and parsed.scheme == "https"
elif mode == "tailscale":
    try:
        address = ipaddress.ip_address(bind)
    except ValueError:
        valid = False
    else:
        valid = (
            address.version == 4
            and not address.is_loopback
            and not address.is_unspecified
            and parsed.scheme == "http"
            and parsed.hostname == bind
            and public_port == port
        )
else:
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

write_config() {
  local enabled=$1 password=$2 mode=$3 bind=$4 public=$5 fingerprint=$6 temporary
  install -d -m 0700 "$CONFIG_DIR"
  temporary=$(mktemp "$CONFIG_DIR/.external-gate.XXXXXX")
  cat >"$temporary" <<EOF
# Shared host-only external-gate configuration. Never put this in a profile env.
EXTERNAL_GATE_ENABLE=$enabled
EXTERNAL_GATE_APPROVAL_PASSWORD=$password
EXTERNAL_GATE_APPROVAL_MODE=$mode
EXTERNAL_GATE_APPROVAL_BIND_ADDR=$bind
EXTERNAL_GATE_PUBLIC_URL=$public
EXTERNAL_GATE_SSH_KEY_FINGERPRINT=$fingerprint
EOF
  chmod 0600 "$temporary"
  mv "$temporary" "$CONFIG_FILE"
}

validate_agent() {
  [[ -n "${SSH_AUTH_SOCK:-}" && -S "$SSH_AUTH_SOCK" ]] || fatal "SSH_AUTH_SOCK must name a dedicated SSH-agent socket"
  [[ "$(stat -Lc '%u' "$SSH_AUTH_SOCK" 2>/dev/null)" == "$(id -u)" ]] || fatal "SSH agent socket must be owned by you"
  local identities count fingerprint
  identities=$(SSH_AUTH_SOCK="$SSH_AUTH_SOCK" ssh-add -l 2>/dev/null) || fatal "dedicated SSH agent is locked, empty, or unavailable"
  count=$(printf '%s\n' "$identities" | awk 'NF {count++} END {print count+0}')
  [[ "$count" -eq 1 ]] || fatal "dedicated SSH agent must contain exactly one identity"
  fingerprint=$(printf '%s\n' "$identities" | awk 'NF {print $2; exit}')
  [[ "$fingerprint" == "$EXTERNAL_GATE_SSH_KEY_FINGERPRINT" ]] || fatal "dedicated SSH identity does not match EXTERNAL_GATE_SSH_KEY_FINGERPRINT"
}

validate_tailscale_bind() {
  [[ "$EXTERNAL_GATE_APPROVAL_MODE" == tailscale ]] || return 0
  command -v tailscale >/dev/null || fatal "tailscale is required for direct Tailscale approval mode"
  local addresses
  addresses=$(tailscale ip -4 2>/dev/null) || fatal "could not read the current Tailscale IPv4 address"
  [[ $(printf '%s\n' "$addresses" | awk 'NF {count++} END {print count+0}') -eq 1 ]] \
    || fatal "direct Tailscale mode requires exactly one current Tailscale IPv4 address"
  [[ "$addresses" == "$EXTERNAL_GATE_APPROVAL_BIND_ADDR" ]] \
    || fatal "approval bind must exactly match the current output of tailscale ip -4"
}

legacy_gate_running() {
  local pid_file="$HOME/.local/state/vivarium-push-gate/broker.pid" pid
  [[ -f "$pid_file" ]] || return 1
  read -r pid <"$pid_file" || return 1
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]] || return 1
  tr '\0' ' ' <"/proc/$pid/cmdline" | grep -Fq 'push-gate-broker.py'
}

export_compose_values() {
  local config_source=${1:-$CONFIG_FILE} ssh_source=${2:-${SSH_AUTH_SOCK:-/dev/null}}
  export HOST_UID="$(id -u)"
  export HOST_GID="$(id -g)"
  export EXTERNAL_GATE_STATE_DIR="$STATE_DIR"
  export EXTERNAL_GATE_SOCKET_DIR="$SOCKET_DIR"
  export EXTERNAL_GATE_CONFIG_FILE="$config_source"
  export EXTERNAL_GATE_SSH_AUTH_SOCK="$ssh_source"
  export EXTERNAL_GATE_APPROVAL_BIND_ADDR
}

gate_compose() {
  docker compose -f "$COMPOSE_FILE" -p vivarium-external-gate "$@"
}

wait_healthy() {
  local attempt
  for attempt in {1..60}; do
    if curl --silent --show-error --fail --max-time 2 \
      --unix-socket "$SOCKET_DIR/request.sock" http://localhost/healthz >/dev/null 2>&1 \
      && curl --silent --show-error --fail --max-time 2 \
        "http://$EXTERNAL_GATE_APPROVAL_BIND_ADDR:$APPROVAL_PORT/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  gate_compose logs --tail 200 --no-log-prefix >&2 || true
  fatal "external gate failed its Unix or approval liveness check"
}

start_locked() {
  local always_recreate=${1:-false}
  read_config
  if [[ "$EXTERNAL_GATE_ENABLE" != true ]]; then
    echo "[external-gate] disabled"
    return 0
  fi
  require_commands
  validate_agent
  validate_tailscale_bind
  legacy_gate_running && fatal "legacy push gate is still running; stop it before starting the external gate"
  install -d -m 0700 "$STATE_DIR" "$SOCKET_DIR"
  export_compose_values

  local socket_identity config_digest desired previous=""
  socket_identity=$(stat -Lc '%n:%d:%i' "$SSH_AUTH_SOCK")
  config_digest=$(sha256sum "$CONFIG_FILE" | awk '{print $1}')
  desired="$socket_identity:$config_digest"
  [[ -f "$RUNTIME_FILE" ]] && read -r previous <"$RUNTIME_FILE" || true

  local arguments=(up -d --build)
  if [[ "$always_recreate" == true || "$desired" != "$previous" ]]; then
    arguments+=(--force-recreate)
  fi
  gate_compose "${arguments[@]}" || fatal "could not build or start the external gate"
  wait_healthy
  printf '%s\n' "$desired" >"$RUNTIME_FILE"
  chmod 0600 "$RUNTIME_FILE"
  echo "[external-gate] running at $EXTERNAL_GATE_PUBLIC_URL"
}

stop_locked() {
  command -v docker >/dev/null 2>&1 || fatal "docker is required to verify that the external gate stopped"
  docker info >/dev/null 2>&1 || fatal "Docker is unavailable; cannot verify that the external gate stopped"
  if ! docker container inspect vivarium-external-gate >/dev/null 2>&1; then
    rm -f "$SOCKET_DIR/request.sock"
    return 0
  fi
  docker compose version >/dev/null 2>&1 || fatal "Docker Compose v2 is required to stop the external gate"
  install -d -m 0700 "$STATE_DIR" "$SOCKET_DIR"
  EXTERNAL_GATE_APPROVAL_BIND_ADDR=127.0.0.1
  export_compose_values /dev/null /dev/null
  gate_compose down --remove-orphans || fatal "could not stop the external gate"
  ! docker container inspect vivarium-external-gate >/dev/null 2>&1 \
    || fatal "external-gate container is still present after stop"
  rm -f "$SOCKET_DIR/request.sock"
}

enable_locked() {
  require_commands
  local generated=false password mode bind public fingerprint identities
  if [[ -f "$CONFIG_FILE" ]]; then
    read_config
    password=$EXTERNAL_GATE_APPROVAL_PASSWORD
    mode=$EXTERNAL_GATE_APPROVAL_MODE
    bind=$EXTERNAL_GATE_APPROVAL_BIND_ADDR
    public=$EXTERNAL_GATE_PUBLIC_URL
    fingerprint=$EXTERNAL_GATE_SSH_KEY_FINGERPRINT
  else
    [[ -n "${SSH_AUTH_SOCK:-}" && -S "$SSH_AUTH_SOCK" ]] || fatal "set SSH_AUTH_SOCK to a dedicated one-key agent before enabling"
    identities=$(SSH_AUTH_SOCK="$SSH_AUTH_SOCK" ssh-add -l 2>/dev/null) || fatal "dedicated SSH agent is locked, empty, or unavailable"
    [[ $(printf '%s\n' "$identities" | awk 'NF {count++} END {print count+0}') -eq 1 ]] \
      || fatal "dedicated SSH agent must contain exactly one identity"
    fingerprint=$(printf '%s\n' "$identities" | awk 'NF {print $2; exit}')
    password=$(random_password)
    mode=loopback
    bind=127.0.0.1
    public="http://127.0.0.1:$APPROVAL_PORT"
    generated=true
  fi
  write_config true "$password" "$mode" "$bind" "$public" "$fingerprint"
  start_locked
  if [[ "$generated" == true ]]; then
    printf '\n[external-gate] approval login (shown once; save it):\n\n    username: vivarium\n    password: %s\n\n' "$password"
  fi
  echo "[external-gate] enabled with one dedicated SSH identity"
}

disable_locked() {
  stop_locked
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "[external-gate] disabled"
    return 0
  fi
  if (read_config >/dev/null 2>&1); then
    read_config
    write_config false "$EXTERNAL_GATE_APPROVAL_PASSWORD" "$EXTERNAL_GATE_APPROVAL_MODE" \
      "$EXTERNAL_GATE_APPROVAL_BIND_ADDR" "$EXTERNAL_GATE_PUBLIC_URL" "$EXTERNAL_GATE_SSH_KEY_FINGERPRINT"
    echo "[external-gate] disabled; state and configuration were preserved"
    return 0
  fi
  local backup="$CONFIG_FILE.invalid"
  [[ ! -e "$backup" ]] || backup="$CONFIG_FILE.invalid.$(date +%s)"
  mv "$CONFIG_FILE" "$backup" || fatal "gate stopped, but malformed configuration could not be preserved"
  chmod 0600 "$backup"
  echo "[external-gate] disabled; malformed configuration was preserved at $backup"
}

status_locked() {
  read_config
  echo "[external-gate] enabled: $EXTERNAL_GATE_ENABLE"
  echo "[external-gate] URL: $EXTERNAL_GATE_PUBLIC_URL"
  if command -v docker >/dev/null 2>&1 \
    && [[ "$(docker container inspect -f '{{.State.Running}}' vivarium-external-gate 2>/dev/null || true)" == true ]] \
    && [[ -S "$SOCKET_DIR/request.sock" ]]; then
    echo "[external-gate] running"
  else
    echo "[external-gate] stopped"
    return 1
  fi
}

password_reset_locked() {
  read_config
  local password
  password=$(random_password)
  write_config "$EXTERNAL_GATE_ENABLE" "$password" "$EXTERNAL_GATE_APPROVAL_MODE" \
    "$EXTERNAL_GATE_APPROVAL_BIND_ADDR" "$EXTERNAL_GATE_PUBLIC_URL" "$EXTERNAL_GATE_SSH_KEY_FINGERPRINT"
  if [[ "$EXTERNAL_GATE_ENABLE" == true ]]; then
    start_locked true
  fi
  printf '[external-gate] new approval login (shown once):\n\n    username: vivarium\n    password: %s\n' "$password"
}

logs_locked() {
  read_config
  command -v docker >/dev/null || fatal "docker is required"
  docker compose version >/dev/null 2>&1 || fatal "Docker Compose v2 is required"
  SSH_AUTH_SOCK="${SSH_AUTH_SOCK:-/dev/null}"
  export_compose_values
  flock -u 9
  gate_compose logs --tail 200 --no-log-prefix
}

lock_lifecycle
case "${1:-}" in
  enable) enable_locked ;;
  start) start_locked ;;
  stop)
    stop_locked
    echo "[external-gate] stopped"
    ;;
  status) status_locked ;;
  disable) disable_locked ;;
  password-reset) password_reset_locked ;;
  logs) logs_locked ;;
  *) echo "usage: $0 {enable|start|stop|status|disable|password-reset|logs}" >&2; exit 2 ;;
esac
