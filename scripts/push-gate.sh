#!/usr/bin/env bash
# Manage one host-user approval broker shared by every Vivarium profile.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
CONFIG_DIR="$HOME/.config/vivarium"
CONFIG_FILE="$CONFIG_DIR/push-gate.env"
STATE_DIR="$HOME/.local/state/vivarium-push-gate"
SOCKET_DIR="$HOME/.local/share/vivarium-push-gate"
INSTALL_DIR="$HOME/.local/lib/vivarium"
BROKER="$INSTALL_DIR/push-gate-broker.py"
PID_FILE="$STATE_DIR/broker.pid"
LOG_FILE="$STATE_DIR/broker.log"
RUNTIME_FILE="$STATE_DIR/runtime-config"

die() { echo "[FATAL] $* Disable with: ./scripts/push-gate.sh disable" >&2; exit 1; }
random_password() { od -An -N24 -tx1 /dev/urandom | tr -d ' \n'; }

read_config() {
  [[ -f "$CONFIG_FILE" ]] || die "push gate is not enabled; run: $0 enable"
  [[ "$(stat -c '%a:%u' "$CONFIG_FILE" 2>/dev/null)" == "600:$(id -u)" ]] || die "$CONFIG_FILE must be owned by you with mode 0600"
  PUSH_GATE_ENABLE= PUSH_GATE_APPROVAL_PASSWORD= PUSH_GATE_APPROVAL_BIND_ADDR= PUSH_GATE_PUBLIC_URL=
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    key=${line%%=*}; value=${line#*=}
    case "$key" in
      PUSH_GATE_ENABLE|PUSH_GATE_APPROVAL_PASSWORD|PUSH_GATE_APPROVAL_BIND_ADDR|PUSH_GATE_PUBLIC_URL)
        [[ -z "${!key}" ]] || die "duplicate $key in $CONFIG_FILE"
        printf -v "$key" '%s' "$value"
        ;;
      *) die "unknown setting $key in $CONFIG_FILE" ;;
    esac
  done <"$CONFIG_FILE"
  [[ "$PUSH_GATE_ENABLE" == true || "$PUSH_GATE_ENABLE" == false ]] || die "invalid PUSH_GATE_ENABLE"
  [[ ${#PUSH_GATE_APPROVAL_PASSWORD} -ge 20 ]] || die "PUSH_GATE_APPROVAL_PASSWORD must contain at least 20 characters"
  [[ -n "$PUSH_GATE_APPROVAL_BIND_ADDR" && "$PUSH_GATE_APPROVAL_BIND_ADDR" != 0.0.0.0 && "$PUSH_GATE_APPROVAL_BIND_ADDR" != :: ]] || die "bind address must be one specific trusted IP"
  [[ "$PUSH_GATE_PUBLIC_URL" == http://* || "$PUSH_GATE_PUBLIC_URL" == https://* ]] || die "invalid PUSH_GATE_PUBLIC_URL"
}

write_config() {
  local password=$1 bind=$2 public=$3 enabled=$4 tmp
  install -d -m 0700 "$CONFIG_DIR"
  tmp=$(mktemp "$CONFIG_DIR/.push-gate.XXXXXX")
  cat >"$tmp" <<EOF
# Shared by every Vivarium profile. Host-only; never mounted into a container.
PUSH_GATE_ENABLE=$enabled
PUSH_GATE_APPROVAL_PASSWORD=$password
PUSH_GATE_APPROVAL_BIND_ADDR=$bind
PUSH_GATE_PUBLIC_URL=$public
EOF
  chmod 0600 "$tmp"
  mv "$tmp" "$CONFIG_FILE"
}

running() {
  BROKER_PID=
  [[ -f "$PID_FILE" ]] || return 1
  read -r BROKER_PID <"$PID_FILE" || return 1
  [[ "$BROKER_PID" =~ ^[0-9]+$ ]] && kill -0 "$BROKER_PID" 2>/dev/null || return 1
  [[ "$(awk '{print $3}' "/proc/$BROKER_PID/stat" 2>/dev/null)" != Z ]] || return 1
  tr '\0' ' ' <"/proc/$BROKER_PID/cmdline" 2>/dev/null | grep -Fq "$BROKER"
}

stop_broker() {
  if running; then
    kill -TERM "$BROKER_PID" 2>/dev/null || true
    for _ in {1..50}; do running || break; sleep 0.1; done
    running && die "broker did not stop"
  fi
  rm -f "$PID_FILE" "$SOCKET_DIR/request.sock"
}

start() {
  read_config
  [[ "$PUSH_GATE_ENABLE" == true ]] || { echo "[push-gate] disabled"; return 0; }
  command -v python3 >/dev/null || die "python3 is required."
  command -v git >/dev/null || die "git is required."
  command -v ssh >/dev/null || die "ssh is required."
  [[ -x "$BROKER" ]] || die "broker is not installed; rerun: $0 enable"
  install -d -m 0700 "$STATE_DIR" "$SOCKET_DIR"
  local previous="" desired
  desired="${SSH_AUTH_SOCK:-}|$PUSH_GATE_APPROVAL_BIND_ADDR|$PUSH_GATE_PUBLIC_URL"
  [[ -f "$RUNTIME_FILE" ]] && read -r previous <"$RUNTIME_FILE" || true
  if running && [[ -S "$SOCKET_DIR/request.sock" && "$previous" == "$desired" ]]; then
    echo "[push-gate] running at $PUSH_GATE_PUBLIC_URL"
    return 0
  fi
  stop_broker
  : >"$LOG_FILE"; chmod 0600 "$LOG_FILE"
  nohup env -i HOME="$HOME" PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PUSH_GATE_PASSWORD="$PUSH_GATE_APPROVAL_PASSWORD" ${SSH_AUTH_SOCK:+SSH_AUTH_SOCK="$SSH_AUTH_SOCK"} \
    /usr/bin/python3 "$BROKER" --state "$STATE_DIR" --socket "$SOCKET_DIR/request.sock" \
    --listen "$PUSH_GATE_APPROVAL_BIND_ADDR:7843" --public-url "$PUSH_GATE_PUBLIC_URL" \
    >>"$LOG_FILE" 2>&1 </dev/null &
  BROKER_PID=$!
  printf '%s\n' "$BROKER_PID" >"$PID_FILE"; chmod 0600 "$PID_FILE"
  printf '%s\n' "$desired" >"$RUNTIME_FILE"; chmod 0600 "$RUNTIME_FILE"
  for _ in {1..50}; do
    [[ -S "$SOCKET_DIR/request.sock" ]] && { echo "[push-gate] running at $PUSH_GATE_PUBLIC_URL"; return 0; }
    kill -0 "$BROKER_PID" 2>/dev/null || break
    sleep 0.1
  done
  rm -f "$PID_FILE"
  die "broker failed to start; inspect $LOG_FILE"
}

enable() {
  local generated=false password bind public
  if [[ -f "$CONFIG_FILE" ]]; then
    read_config
    password=$PUSH_GATE_APPROVAL_PASSWORD; bind=$PUSH_GATE_APPROVAL_BIND_ADDR; public=$PUSH_GATE_PUBLIC_URL
  else
    password=$(random_password); bind=127.0.0.1; public=http://127.0.0.1:7843; generated=true
  fi
  install -d -m 0700 "$INSTALL_DIR"
  install -m 0700 scripts/push-gate-broker.py "$BROKER"
  write_config "$password" "$bind" "$public" true
  start
  if $generated; then
    printf '\n[push-gate] approval login (shown once; save it):\n\n    username: vivarium\n    password: %s\n\n' "$password"
  fi
  echo "[push-gate] enabled; it uses the host user's existing GitHub SSH access"
}

disable() {
  if [[ -f "$CONFIG_FILE" ]]; then
    read_config
    stop_broker
    write_config "$PUSH_GATE_APPROVAL_PASSWORD" "$PUSH_GATE_APPROVAL_BIND_ADDR" "$PUSH_GATE_PUBLIC_URL" false
  fi
  echo "[push-gate] disabled; pending requests were preserved"
}

status() {
  read_config
  echo "[push-gate] enabled: $PUSH_GATE_ENABLE"
  echo "[push-gate] URL: $PUSH_GATE_PUBLIC_URL"
  if running && [[ -S "$SOCKET_DIR/request.sock" ]]; then echo "[push-gate] running (pid $BROKER_PID)"; else echo "[push-gate] stopped"; return 1; fi
}

password_reset() {
  read_config
  local password
  password=$(random_password)
  write_config "$password" "$PUSH_GATE_APPROVAL_BIND_ADDR" "$PUSH_GATE_PUBLIC_URL" "$PUSH_GATE_ENABLE"
  stop_broker
  [[ "$PUSH_GATE_ENABLE" == true ]] && start
  printf '[push-gate] new approval login (shown once):\n\n    username: vivarium\n    password: %s\n' "$password"
}

case "${1:-}" in
  enable) enable ;;
  start) start ;;
  stop) read_config; stop_broker; echo "[push-gate] stopped" ;;
  status) status ;;
  disable) disable ;;
  password-reset) password_reset ;;
  *) echo "usage: $0 {enable|start|stop|status|disable|password-reset}" >&2; exit 2 ;;
esac
