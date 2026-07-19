"""Run the single hardened external-gate daemon."""

from __future__ import annotations

import contextlib
import fcntl
import ipaddress
import logging
import os
import signal
import stat
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .gate import AgentHandler, BrowserHTTPServer, BrowserHandler, Gate, GateConfig, UnixHTTPServer
from .git_push import GitPushRoute

STATE_DIR = Path("/var/lib/vivarium-external-gate")
SOCKET_PATH = Path("/run/vivarium-external-gate/request.sock")
CONFIG_PATH = Path("/run/vivarium-external-gate-config/external-gate.env")
SCRATCH_DIR = Path("/var/tmp/vivarium-external-gate")
SSH_SOCKET = Path("/run/vivarium-external-gate-ssh/agent.sock")
KNOWN_HOSTS = Path(__file__).with_name("github_known_hosts")
APPROVAL_PORT = 7843
CONFIG_KEYS = {
    "EXTERNAL_GATE_ENABLE",
    "EXTERNAL_GATE_APPROVAL_PASSWORD",
    "EXTERNAL_GATE_APPROVAL_MODE",
    "EXTERNAL_GATE_APPROVAL_BIND_ADDR",
    "EXTERNAL_GATE_PUBLIC_URL",
    "EXTERNAL_GATE_SSH_KEY_FINGERPRINT",
}


def read_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("invalid configuration line")
        key, value = line.split("=", 1)
        if key not in CONFIG_KEYS or key in values:
            raise ValueError("unknown or duplicate configuration key")
        values[key] = value
    if set(values) != CONFIG_KEYS:
        raise ValueError("missing configuration key")
    return values


def validate_transport(values: dict[str, str]) -> str:
    if values["EXTERNAL_GATE_ENABLE"] != "true":
        raise ValueError("external gate is not enabled")
    mode = values["EXTERNAL_GATE_APPROVAL_MODE"]
    bind = values["EXTERNAL_GATE_APPROVAL_BIND_ADDR"]
    public = values["EXTERNAL_GATE_PUBLIC_URL"]
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
        raise ValueError("public URL must be one origin")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise ValueError("invalid public URL port") from error
    if mode == "loopback":
        if bind != "127.0.0.1" or public != f"http://127.0.0.1:{APPROVAL_PORT}":
            raise ValueError("loopback listener and public origin must match")
    elif mode == "proxy":
        if bind != "127.0.0.1" or parsed.scheme != "https":
            raise ValueError("proxy mode requires loopback plus HTTPS")
    elif mode == "tailscale":
        try:
            address = ipaddress.ip_address(bind)
        except ValueError as error:
            raise ValueError("Tailscale bind must be an IPv4 literal") from error
        if address.version != 4 or address.is_loopback or address.is_unspecified:
            raise ValueError("Tailscale bind must be one non-loopback IPv4 address")
        if parsed.scheme != "http" or parsed.hostname != bind or port != APPROVAL_PORT:
            raise ValueError("Tailscale listener and public origin must match")
    else:
        raise ValueError("invalid approval mode")
    password = values["EXTERNAL_GATE_APPROVAL_PASSWORD"]
    if len(password) < 20:
        raise ValueError("approval password is too short")
    fingerprint = values["EXTERNAL_GATE_SSH_KEY_FINGERPRINT"]
    if not fingerprint.startswith("SHA256:") or len(fingerprint) < 20:
        raise ValueError("invalid SSH key fingerprint")
    return public


def acquire_daemon_lock(state_dir: Path) -> int:
    lock_fd = os.open(state_dir / "daemon.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(lock_fd)
        raise
    return lock_fd


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    try:
        lock_fd = acquire_daemon_lock(STATE_DIR)
    except BlockingIOError as error:
        raise SystemExit("[FATAL] external gate already has a state owner") from error
    try:
        config_stat = CONFIG_PATH.stat()
        if config_stat.st_uid != os.getuid() or stat.S_IMODE(config_stat.st_mode) != 0o600:
            raise SystemExit("[FATAL] external-gate configuration must be owned by the gate user with mode 0600")
        socket_stat = SSH_SOCKET.stat()
        if socket_stat.st_uid != os.getuid() or not stat.S_ISSOCK(socket_stat.st_mode):
            raise SystemExit("[FATAL] dedicated SSH-agent socket must be owned by the gate user")
        values = read_config(CONFIG_PATH)
        public_origin = validate_transport(values)
        route = GitPushRoute(
            SCRATCH_DIR,
            SSH_SOCKET,
            values["EXTERNAL_GATE_SSH_KEY_FINGERPRINT"],
            KNOWN_HOSTS,
        )
        route.agent_validator()
        route.cleanup_scratch()
        gate = Gate(
            GateConfig(
                state_dir=STATE_DIR,
                socket_path=SOCKET_PATH,
                public_origin=public_origin,
                password=values["EXTERNAL_GATE_APPROVAL_PASSWORD"],
            ),
            {route.name: route},
        )

        with contextlib.suppress(FileNotFoundError):
            SOCKET_PATH.unlink()
        AgentHandler.gate = gate
        BrowserHandler.gate = gate
        unix_server = UnixHTTPServer(str(SOCKET_PATH), AgentHandler)
        os.chmod(SOCKET_PATH, 0o600)
        browser_server = BrowserHTTPServer(("0.0.0.0", APPROVAL_PORT), BrowserHandler)
        stopping = threading.Event()

        def stop(_signum=None, _frame=None):
            if stopping.is_set():
                return
            stopping.set()
            gate.stop_worker()
            threading.Thread(target=unix_server.shutdown, daemon=True).start()
            threading.Thread(target=browser_server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        threading.Thread(target=unix_server.serve_forever, name="external-gate-unix", daemon=True).start()
        gate.start_worker()
        try:
            browser_server.serve_forever()
        finally:
            gate.stop_worker()
            unix_server.shutdown()
            unix_server.server_close()
            browser_server.server_close()
            with contextlib.suppress(FileNotFoundError):
                SOCKET_PATH.unlink()
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        raise SystemExit("[FATAL] external gate startup failed") from None
