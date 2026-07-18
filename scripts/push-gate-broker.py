#!/usr/bin/env python3
"""Host-side approval gate for one exact GitHub branch push."""

import argparse
import base64
import contextlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import shlex
import signal
import socketserver
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ZERO = "0" * 40
ID_RE = re.compile(r"^[0-9a-f]{32}$")
OID_RE = re.compile(r"^[0-9a-f]{40}$")
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_BUNDLE = 100 * 1024 * 1024
MAX_PENDING = 20
MAX_TOTAL = 500 * 1024 * 1024


def atomic_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def validate_ref(ref: str) -> bool:
    if not ref.startswith("refs/heads/") or len(ref) > 250:
        return False
    name = ref.removeprefix("refs/heads/")
    if not name or name.startswith(".") or name.endswith(("/", ".", ".lock")):
        return False
    return not any(x in name for x in ("..", "//", "@{", " ", "~", "^", ":", "?", "*", "[", "\\")) and all(ord(c) >= 32 and ord(c) != 127 for c in name)


def validate_meta(meta: dict) -> bool:
    return (
        set(meta) == {"profile", "owner", "repo", "ref", "old_oid", "new_oid", "state", "message"}
        and PROFILE_RE.fullmatch(meta["profile"]) is not None
        and OWNER_RE.fullmatch(meta["owner"]) is not None
        and REPO_RE.fullmatch(meta["repo"]) is not None
        and meta["repo"] not in (".", "..")
        and not meta["repo"].endswith(".git")
        and validate_ref(meta["ref"])
        and OID_RE.fullmatch(meta["old_oid"]) is not None
        and OID_RE.fullmatch(meta["new_oid"]) is not None
        and meta["new_oid"] != ZERO
        and meta["new_oid"] != meta["old_oid"]
        and meta["state"] in {"pending", "approved", "denied", "succeeded", "failed"}
        and isinstance(meta["message"], str)
        and len(meta["message"]) <= 300
    )


class Gate:
    def __init__(self, state_dir: Path, socket_path: Path, public_url: str, password: str):
        self.state_dir = state_dir
        self.requests_dir = state_dir / "requests"
        self.quarantine_dir = state_dir / "quarantine"
        self.socket_path = socket_path
        self.public_url = public_url.rstrip("/")
        parsed = urlsplit(self.public_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("public URL must be an HTTP(S) origin")
        self.password = password
        self.csrf_token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.active: set[str] = set()
        for directory in (state_dir, self.requests_dir, self.quarantine_dir, socket_path.parent):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)

    def path(self, request_id: str) -> Path:
        if not ID_RE.fullmatch(request_id):
            raise ValueError("invalid request id")
        return self.requests_dir / request_id

    def load(self, request_id: str) -> dict | None:
        try:
            value = json.loads((self.path(request_id) / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return value if validate_meta(value) else None

    def save(self, request_id: str, meta: dict) -> None:
        if not validate_meta(meta):
            raise ValueError("invalid request state")
        atomic_json(self.path(request_id) / "meta.json", meta)

    def submit(self, headers, body) -> dict:
        fields = {
            "profile": headers.get("X-Vivarium-Profile", "default"),
            "owner": headers.get("X-Vivarium-Owner", ""),
            "repo": headers.get("X-Vivarium-Repo", ""),
            "ref": headers.get("X-Vivarium-Ref", ""),
            "old_oid": headers.get("X-Vivarium-Old-Oid", ""),
            "new_oid": headers.get("X-Vivarium-New-Oid", ""),
            "state": "pending",
            "message": "",
        }
        if not validate_meta(fields):
            raise ValueError("invalid push metadata")
        with self.lock:
            pending = 0
            total = 0
            for entry in self.requests_dir.iterdir():
                meta = self.load(entry.name) if entry.is_dir() else None
                if meta and meta["state"] in ("pending", "approved"):
                    pending += 1
                    with contextlib.suppress(OSError):
                        total += (entry / "repo.bundle").stat().st_size
            if pending >= MAX_PENDING or total + len(body) > MAX_TOTAL:
                raise ValueError("pending request quota reached")
            request_id = secrets.token_hex(16)
            request_dir = self.path(request_id)
            request_dir.mkdir(mode=0o700)
            bundle = request_dir / "repo.bundle"
            with bundle.open("xb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(bundle, 0o600)
            self.save(request_id, fields)
        return {"id": request_id, "approval_url": f"{self.public_url}/r/{request_id}"}

    def decide(self, request_id: str, state: str) -> bool:
        if state not in ("approved", "denied"):
            return False
        with self.lock:
            meta = self.load(request_id)
            if not meta or meta["state"] != "pending":
                return False
            meta["state"] = state
            meta["message"] = "denied by approver" if state == "denied" else ""
            self.save(request_id, meta)
            if state == "denied":
                with contextlib.suppress(OSError):
                    (self.path(request_id) / "repo.bundle").unlink()
        if state == "approved":
            self.start_execution(request_id)
        return True

    def start_execution(self, request_id: str) -> None:
        with self.lock:
            if request_id in self.active:
                return
            meta = self.load(request_id)
            if not meta or meta["state"] != "approved":
                return
            self.active.add(request_id)
        threading.Thread(target=self.execute, args=(request_id,), daemon=True).start()

    def resume(self) -> None:
        for entry in self.requests_dir.iterdir():
            meta = self.load(entry.name) if entry.is_dir() else None
            if meta and meta["state"] == "approved":
                self.start_execution(entry.name)

    def finish(self, request_id: str, state: str, message: str) -> None:
        with self.lock:
            meta = self.load(request_id)
            if meta and meta["state"] == "approved":
                meta["state"] = state
                meta["message"] = message[:300]
                self.save(request_id, meta)
                with contextlib.suppress(OSError):
                    (self.path(request_id) / "repo.bundle").unlink()
            self.active.discard(request_id)

    def execute(self, request_id: str) -> None:
        meta = self.load(request_id)
        if not meta:
            self.finish(request_id, "failed", "stored request is invalid")
            return
        request_dir = self.path(request_id)
        bundle = request_dir / "repo.bundle"
        try:
            with tempfile.TemporaryDirectory(prefix="push-", dir=self.quarantine_dir) as temp:
                repo_dir = Path(temp) / "repo.git"
                self.git(temp, "init", "--bare", "--quiet", str(repo_dir))
                heads = self.git(repo_dir, "bundle", "list-heads", str(bundle)).splitlines()
                if heads != [f"{meta['new_oid']} HEAD"]:
                    raise RuntimeError("bundle does not contain exactly the approved HEAD")
                self.git(repo_dir, "fetch", "--quiet", str(bundle), "HEAD:refs/heads/candidate")
                if self.git(repo_dir, "rev-parse", "refs/heads/candidate").strip() != meta["new_oid"]:
                    raise RuntimeError("bundle commit does not match approval")
                self.git(repo_dir, "fsck", "--strict", "--no-reflogs", meta["new_oid"])
                grep = self.git_result(repo_dir, "grep", "-I", "-q", "-e", "version https://git-lfs.github.com/spec/v1", meta["new_oid"])
                if grep.returncode == 0:
                    raise RuntimeError("Git LFS pushes are not supported")
                if grep.returncode != 1:
                    raise RuntimeError("could not inspect bundle content")
                remote = self.remote_url(meta)
                current = self.remote_oid(repo_dir, remote, meta["ref"])
                if current == meta["new_oid"]:
                    self.finish(request_id, "succeeded", "remote already has the approved commit")
                    return
                if current != meta["old_oid"]:
                    raise RuntimeError("remote branch changed after the request")
                if meta["old_oid"] != ZERO:
                    ancestor = self.git_result(repo_dir, "merge-base", "--is-ancestor", meta["old_oid"], meta["new_oid"])
                    if ancestor.returncode != 0:
                        raise RuntimeError("requested update is not a fast-forward")
                lease = f"--force-with-lease={meta['ref']}:" + ("" if meta["old_oid"] == ZERO else meta["old_oid"])
                result = self.git_result(repo_dir, "push", "--porcelain", "--no-verify", lease, remote, f"{meta['new_oid']}:{meta['ref']}", timeout=180)
                if result.returncode == 0:
                    self.finish(request_id, "succeeded", "exact approved commit pushed")
                    return
                if self.remote_oid(repo_dir, remote, meta["ref"]) == meta["new_oid"]:
                    self.finish(request_id, "succeeded", "remote confirms the approved commit")
                    return
                raise RuntimeError("GitHub rejected the exact approved push")
        except Exception as error:  # Fail closed; detailed Git output is intentionally not exposed.
            self.finish(request_id, "failed", str(error)[:300])

    def remote_url(self, meta: dict) -> str:
        return f"git@github.com:{meta['owner']}/{meta['repo']}.git"

    def remote_oid(self, directory: Path, remote: str, ref: str) -> str:
        output = self.git(directory, "ls-remote", "--refs", remote, ref).strip()
        if not output:
            return ZERO
        lines = output.splitlines()
        if len(lines) != 1:
            raise RuntimeError("unexpected remote ref advertisement")
        fields = lines[0].split()
        if len(fields) != 2 or fields[1] != ref or not OID_RE.fullmatch(fields[0]):
            raise RuntimeError("unexpected remote ref advertisement")
        return fields[0]

    def git_env(self) -> dict:
        env = {
            "HOME": os.environ["HOME"],
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_PROTOCOL_FROM_USER": "0",
            "SSH_ASKPASS": "/bin/false",
            "GIT_SSH_COMMAND": "/usr/bin/ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ClearAllForwardings=yes -o ForwardAgent=no -o PermitLocalCommand=no -o RequestTTY=no",
        }
        if os.environ.get("SSH_AUTH_SOCK"):
            env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
        return env

    def git_result(self, directory, *args, timeout=120):
        command = ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "protocol.allow=never", "-c", "protocol.ssh.allow=always", *args]
        return subprocess.run(command, cwd=directory, env=self.git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)

    def git(self, directory, *args) -> str:
        result = self.git_result(directory, *args)
        if result.returncode != 0:
            raise RuntimeError("Git validation failed")
        return result.stdout


class UnixHTTPServer(socketserver.UnixStreamServer):
    pass


class RequestHandler(BaseHTTPRequestHandler):
    gate: Gate
    server_version = "VivariumPushGate/1"

    def log_message(self, _format, *_args):
        pass

    def do_POST(self):
        if self.path != "/requests":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BUNDLE:
            self.send_error(413)
            return
        if self.headers.get("Content-Type") != "application/x-git-bundle":
            self.send_error(415)
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self.send_error(400)
            return
        try:
            response = self.gate.submit(self.headers, body)
        except (OSError, ValueError) as error:
            self.json_response(409, {"error": str(error)})
            return
        self.json_response(201, response)

    def do_GET(self):
        match = re.fullmatch(r"/requests/([0-9a-f]{32})", self.path)
        if not match:
            self.send_error(404)
            return
        meta = self.gate.load(match.group(1))
        if not meta:
            self.send_error(404)
            return
        self.json_response(200, {"state": meta["state"], "message": meta["message"]})

    def json_response(self, status: int, value: dict):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BrowserHandler(BaseHTTPRequestHandler):
    gate: Gate
    server_version = "VivariumPushGate/1"

    def log_message(self, _format, *_args):
        pass

    def authenticated(self) -> bool:
        header = self.headers.get("Authorization", "")
        try:
            scheme, encoded = header.split(" ", 1)
            user, password = base64.b64decode(encoded, validate=True).decode().split(":", 1)
        except (ValueError, UnicodeError):
            return False
        return scheme == "Basic" and hmac.compare_digest(user, "vivarium") and hmac.compare_digest(password.encode(), self.gate.password.encode())

    def require_auth(self) -> bool:
        if self.authenticated():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Vivarium push approval", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self.require_auth():
            return
        match = re.fullmatch(r"/r/([0-9a-f]{32})", self.path)
        if not match:
            self.send_error(404)
            return
        meta = self.gate.load(match.group(1))
        if not meta:
            self.send_error(404)
            return
        self.page(match.group(1), meta)

    def do_POST(self):
        if not self.require_auth():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        form = parse_qs(self.rfile.read(length).decode()) if 0 < length <= 4096 else {}
        token = form.get("csrf", [""])[0]
        if not hmac.compare_digest(token, self.gate.csrf_token):
            self.send_error(403)
            return
        match = re.fullmatch(r"/r/([0-9a-f]{32})/(approve|deny)", self.path)
        if not match or not self.gate.decide(match.group(1), "approved" if match.group(2) == "approve" else "denied"):
            self.send_error(409)
            return
        self.send_response(303)
        self.send_header("Location", f"/r/{match.group(1)}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_response(self, code, message=None):
        super().send_response(code, message)
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")

    def page(self, request_id: str, meta: dict):
        remote = f"git@github.com:{meta['owner']}/{meta['repo']}.git"
        lease = f"--force-with-lease={meta['ref']}:" + ("" if meta["old_oid"] == ZERO else meta["old_oid"])
        tokens = [
            ("cmd", "git"), ("arg", "push"), ("arg", "--porcelain"), ("arg", "--no-verify"),
            ("lease", shlex.quote(lease)), ("url", shlex.quote(remote)),
            ("ref", shlex.quote(f"{meta['new_oid']}:{meta['ref']}")),
        ]
        command = " ".join(f'<span class="{kind}">{html.escape(text)}</span>' for kind, text in tokens)
        details = "".join(f"<div><dt>{html.escape(label)}</dt><dd><code>{html.escape(value)}</code></dd></div>" for label, value in (
            ("Profile", meta["profile"]), ("Repository", f"{meta['owner']}/{meta['repo']}"),
            ("Branch", meta["ref"]), ("Old commit", meta["old_oid"]), ("New commit", meta["new_oid"]),
            ("State", meta["state"]),
        ))
        actions = ""
        if meta["state"] == "pending":
            csrf = html.escape(self.gate.csrf_token)
            actions = f'<div class="actions"><form method="post" action="/r/{request_id}/approve"><input type="hidden" name="csrf" value="{csrf}"><button class="approve">Approve once</button></form><form method="post" action="/r/{request_id}/deny"><input type="hidden" name="csrf" value="{csrf}"><button class="deny">Deny</button></form></div>'
        elif meta["message"]:
            actions = f'<p class="message">{html.escape(meta["message"])}</p>'
        body = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Push approval</title><style>
:root{{color-scheme:dark;--bg:#0b0d10;--panel:#15191f;--line:#29313b;--text:#eef2f7;--muted:#9ba7b6;--green:#37d67a;--red:#ff6376}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif}}main{{width:min(820px,calc(100% - 32px));margin:7vh auto}}h1{{font-size:clamp(2rem,6vw,3.5rem);margin:.2rem 0 2rem}}.eyebrow,.note,.message,dt{{color:var(--muted)}}section,dl{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px}}pre{{overflow:auto;background:#080a0d;padding:18px;border-radius:10px}}code{{font-family:ui-monospace,monospace}}.cmd{{color:var(--green)}}.arg{{color:#75a7ff}}.lease{{color:#ff9e78}}.url{{color:#d7a8ff}}.ref{{color:#ffd479}}dl{{margin-top:18px}}dl div{{display:grid;grid-template-columns:130px 1fr;padding:9px 0;border-bottom:1px solid var(--line)}}dd{{margin:0;overflow-wrap:anywhere}}.actions{{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-top:22px}}button{{width:100%;border:0;border-radius:12px;padding:18px;font:inherit;font-weight:800;cursor:pointer}}.approve{{background:var(--green);color:#07140c}}.deny{{background:var(--red);color:#1b070a}}@media(max-width:560px){{dl div,.actions{{grid-template-columns:1fr}}}}</style></head><body><main><p class="eyebrow">One-time push approval</p><h1>{html.escape(meta['owner'])}/{html.escape(meta['repo'])}</h1><section><h2>Exact host push</h2><p class="note">Derived from the frozen request.</p><pre><code>{command}</code></pre></section><dl>{details}</dl>{actions}</main></body></html>'''.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--listen", required=True)
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--password", default=os.environ.get("PUSH_GATE_PASSWORD"))
    args = parser.parse_args()
    if not args.password or len(args.password) < 20:
        raise SystemExit("PUSH_GATE_PASSWORD must contain at least 20 characters")
    host, separator, port = args.listen.rpartition(":")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise SystemExit("listen must use one specific IPv4 address")
    if not separator or address.version != 4 or address.is_unspecified:
        raise SystemExit("listen must use one specific IPv4 address")
    gate = Gate(Path(args.state), Path(args.socket), args.public_url, args.password)
    with contextlib.suppress(FileNotFoundError):
        gate.socket_path.unlink()
    RequestHandler.gate = gate
    BrowserHandler.gate = gate
    unix_server = UnixHTTPServer(str(gate.socket_path), RequestHandler)
    os.chmod(gate.socket_path, 0o600)
    browser_server = ThreadingHTTPServer((host.strip("[]"), int(port)), BrowserHandler)
    stopping = threading.Event()

    def stop(_signum=None, _frame=None):
        if stopping.is_set():
            return
        stopping.set()
        threading.Thread(target=unix_server.shutdown, daemon=True).start()
        threading.Thread(target=browser_server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=unix_server.serve_forever, daemon=True).start()
    gate.resume()
    try:
        browser_server.serve_forever()
    finally:
        unix_server.shutdown()
        unix_server.server_close()
        browser_server.server_close()
        with contextlib.suppress(FileNotFoundError):
            gate.socket_path.unlink()


if __name__ == "__main__":
    main()
