import base64
import importlib.util
import re
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import urlencode
from http.server import ThreadingHTTPServer
from pathlib import Path

MODULE = Path(__file__).parents[1] / "scripts" / "push-gate-broker.py"
spec = importlib.util.spec_from_file_location("push_gate_broker", MODULE)
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)


class LocalGate(broker.Gate):
    def __init__(self, *args, remote, **kwargs):
        super().__init__(*args, **kwargs)
        self.remote = str(remote)

    def remote_url(self, _meta):
        return self.remote

    def git_result(self, directory, *args, timeout=120):
        command = ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never", "-c", "protocol.file.allow=always", *args]
        env = self.git_env()
        env.pop("GIT_SSH_COMMAND", None)
        return subprocess.run(command, cwd=directory, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)


class PushGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.gate = broker.Gate(root / "state", root / "socket" / "request.sock", "http://127.0.0.1:7843", "a" * 24)
        self.headers = {
            "X-Vivarium-Profile": "work",
            "X-Vivarium-Owner": "example",
            "X-Vivarium-Repo": "repo",
            "X-Vivarium-Ref": "refs/heads/feature",
            "X-Vivarium-Old-Oid": "0" * 40,
            "X-Vivarium-New-Oid": "1" * 40,
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_validation_rejects_non_branch_and_delete(self):
        meta = {"profile": "work", "owner": "example", "repo": "repo", "ref": "refs/tags/v1", "old_oid": "0" * 40, "new_oid": "1" * 40, "state": "pending", "message": ""}
        self.assertFalse(broker.validate_meta(meta))
        meta["ref"] = "refs/heads/main"
        meta["new_oid"] = "0" * 40
        self.assertFalse(broker.validate_meta(meta))

    def test_decision_is_one_use(self):
        submitted = self.gate.submit(self.headers, b"bundle")
        request_id = submitted["id"]
        self.gate.start_execution = lambda _request_id: None
        self.assertTrue(self.gate.decide(request_id, "approved"))
        self.assertFalse(self.gate.decide(request_id, "denied"))
        self.assertEqual(self.gate.load(request_id)["state"], "approved")

    def test_clean_bundle_create_and_fast_forward(self):
        root = Path(self.temp.name)
        source, remote = root / "source", root / "remote.git"
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
        (source / "file").write_text("one\n")
        subprocess.run(["git", "-C", str(source), "add", "file"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "one"], check=True)

        gate = LocalGate(root / "local-state", root / "local-socket" / "request.sock", "http://127.0.0.1:7843", "a" * 24, remote=remote)
        gate.start_execution = lambda _request_id: None
        old = "0" * 40
        first = None
        for content in ("one\n", "two\n"):
            if content == "two\n":
                old = new
                (source / "file").write_text(content)
                subprocess.run(["git", "-C", str(source), "commit", "-qam", "two"], check=True)
            new = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
            bundle = root / "test.bundle"
            subprocess.run(["git", "-C", str(source), "bundle", "create", str(bundle), "HEAD"], check=True)
            headers = dict(self.headers, **{"X-Vivarium-Old-Oid": old, "X-Vivarium-New-Oid": new})
            request_id = gate.submit(headers, bundle.read_bytes())["id"]
            self.assertTrue(gate.decide(request_id, "approved"))
            gate.execute(request_id)
            self.assertEqual(gate.load(request_id)["state"], "succeeded")
            self.assertEqual(subprocess.check_output(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/feature"], text=True).strip(), new)
            first = first or new
            bundle.unlink()

        (source / "file").write_text("three\n")
        subprocess.run(["git", "-C", str(source), "commit", "-qam", "three"], check=True)
        third = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        bundle = root / "stale.bundle"
        subprocess.run(["git", "-C", str(source), "bundle", "create", str(bundle), "HEAD"], check=True)
        stale = dict(self.headers, **{"X-Vivarium-Old-Oid": first, "X-Vivarium-New-Oid": third})
        request_id = gate.submit(stale, bundle.read_bytes())["id"]
        self.assertTrue(gate.decide(request_id, "approved"))
        gate.execute(request_id)
        self.assertEqual(gate.load(request_id)["state"], "failed")

    def test_browser_requires_auth_and_origin(self):
        request_id = self.gate.submit(self.headers, b"bundle")["id"]
        self.gate.start_execution = lambda _request_id: None
        broker.BrowserHandler.gate = self.gate
        server = ThreadingHTTPServer(("127.0.0.1", 0), broker.BrowserHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        auth = "Basic " + base64.b64encode(("vivarium:" + "a" * 24).encode()).decode()
        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(base + f"/r/{request_id}")
            self.assertEqual(denied.exception.code, 401)

            page = urllib.request.Request(base + f"/r/{request_id}", headers={"Authorization": auth})
            markup = urllib.request.urlopen(page).read().decode()
            csrf = re.search(r'name="csrf" value="([^"]+)"', markup).group(1)
            form = urlencode({"csrf": csrf}).encode()

            wrong_origin = urllib.request.Request(base + f"/r/{request_id}/approve", data=form, method="POST", headers={"Authorization": auth, "Origin": "http://evil.invalid"})
            with self.assertRaises(urllib.error.HTTPError) as forbidden:
                urllib.request.urlopen(wrong_origin)
            self.assertEqual(forbidden.exception.code, 403)
            self.assertEqual(self.gate.load(request_id)["state"], "pending")

            self.gate.origin = base
            approved = urllib.request.Request(base + f"/r/{request_id}/approve", data=form, method="POST", headers={"Authorization": auth, "Origin": base})
            opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
            response = opener.open(approved)
            self.assertEqual(response.status, 200)
            self.assertEqual(self.gate.load(request_id)["state"], "approved")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
