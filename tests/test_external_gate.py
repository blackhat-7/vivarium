import base64
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

import external_gate.gate as core
from external_gate.__main__ import CONFIG_KEYS, acquire_daemon_lock, read_config, validate_transport
from external_gate.gate import (
    ActionResult,
    ActionRoute,
    AgentHandler,
    BrowserHTTPServer,
    BrowserHandler,
    Gate,
    GateConfig,
    GateError,
    UnixHTTPServer,
)


class Clock:
    def __init__(self, value=1_000.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeRoute(ActionRoute):
    name = "fake.v1"
    content_type = "application/octet-stream"
    max_body_bytes = 1024 * 1024
    metadata_headers = ("X-Vivarium-Value",)

    def __init__(self):
        self.executions = 0
        self.reconciliations = 0
        self.result = ActionResult("succeeded", "fake_succeeded", "fake action succeeded")
        self.reconcile_result = ActionResult("failed", "fake_not_applied", "fake action was not applied")

    def freeze(self, metadata, _body_path, digest, size):
        value = metadata["X-Vivarium-Value"]
        if not value:
            raise ValueError("empty value")
        return {"value": value, "digest": digest, "size": size}

    def decode(self, frozen):
        if not isinstance(frozen, dict) or set(frozen) != {"value", "digest", "size"}:
            raise ValueError("invalid action")
        if not isinstance(frozen["value"], str) or not isinstance(frozen["digest"], str):
            raise ValueError("invalid action")
        if not isinstance(frozen["size"], int):
            raise ValueError("invalid action")
        return frozen

    def describe(self, action):
        return [("Value", action["value"])]

    def execute(self, _action, _body_path):
        self.executions += 1
        return self.result

    def reconcile(self, _action):
        self.reconciliations += 1
        return self.reconcile_result


class ExternalGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.monotonic = Clock()
        self.route = FakeRoute()
        self.config = GateConfig(
            state_dir=self.root / "state",
            socket_path=self.root / "socket" / "request.sock",
            public_origin="http://127.0.0.1:7843",
            password="a" * 32,
            decision_ttl=100,
            uncertain_ttl=100,
            maintenance_interval=100,
        )
        self.gate = Gate(
            self.config,
            {self.route.name: self.route},
            wall_clock=self.clock,
            monotonic=self.monotonic,
            fatal_exit=lambda: None,
        )

    def tearDown(self):
        self.gate.stop_worker()
        self.temp.cleanup()

    def submit(self, body=b"payload", value="work", gate=None):
        gate = gate or self.gate
        offset = 0

        def read(amount):
            nonlocal offset
            chunk = body[offset:offset + amount]
            offset += len(chunk)
            return chunk

        return gate.submit("fake.v1", {"X-Vivarium-Value": value}, len(body), read, gate.monotonic() + 10)

    def wait_state(self, request_id, state, gate=None):
        gate = gate or self.gate
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if gate.load(request_id)["state"] == state:
                return
            time.sleep(0.01)
        self.fail(f"request did not reach {state}")

    def test_fake_route_submission_and_one_shot_approval(self):
        submitted = self.submit()
        request_id = submitted["id"]
        self.assertEqual(submitted["approval_url"], f"http://127.0.0.1:7843/r/{request_id}")
        record = self.gate.load(request_id)
        self.assertEqual(record["state"], "pending")
        self.assertEqual(set((self.gate.request_path(request_id)).iterdir()), {
            self.gate.request_path(request_id) / "body",
            self.gate.request_path(request_id) / "request.json",
        })
        self.assertTrue(self.gate.decide(request_id, "approve"))
        self.assertFalse(self.gate.decide(request_id, "deny"))
        self.gate.start_worker()
        self.wait_state(request_id, "succeeded")
        body_path = self.gate.request_path(request_id) / "body"
        deadline = time.monotonic() + 1
        while body_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.route.executions, 1)
        self.assertFalse(body_path.exists())

    def test_decision_deadline_is_exclusive(self):
        request_id = self.submit()["id"]
        self.clock.value = self.gate.load(request_id)["decision_deadline"]
        self.assertFalse(self.gate.decide(request_id, "approve"))
        self.assertEqual(self.gate.load(request_id)["state"], "expired")

    def test_short_and_slow_uploads_release_reservation(self):
        with self.assertRaises(GateError):
            self.gate.submit("fake.v1", {"X-Vivarium-Value": "x"}, 5, lambda _amount: b"", self.monotonic() + 1)
        self.assertEqual((self.gate.reserved_slots, self.gate.reserved_bytes), (0, 0))

        def too_slow(_amount):
            self.monotonic.value += 2
            return b"x"

        with self.assertRaises(GateError) as raised:
            self.gate.submit("fake.v1", {"X-Vivarium-Value": "x"}, 2, too_slow, self.monotonic() + 1)
        self.assertEqual(raised.exception.code, "upload_deadline")
        self.assertEqual((self.gate.reserved_slots, self.gate.reserved_bytes), (0, 0))

    def test_failed_upload_cleanup_failure_stops_gate(self):
        exits = []
        self.gate.fatal_exit = lambda: exits.append(True)
        with mock.patch("external_gate.gate.shutil.rmtree", side_effect=OSError("cleanup failed")):
            with self.assertRaises(OSError):
                self.gate.submit(
                    "fake.v1", {"X-Vivarium-Value": "x"}, 5,
                    lambda _amount: b"", self.monotonic() + 1,
                )
        self.assertEqual(exits, [True])
        self.assertFalse(self.gate.healthy())
        self.assertEqual((self.gate.reserved_slots, self.gate.reserved_bytes), (0, 0))

    def test_digest_change_prevents_execution(self):
        request_id = self.submit()["id"]
        self.assertTrue(self.gate.decide(request_id, "approve"))
        (self.gate.request_path(request_id) / "body").write_bytes(b"changed")
        record = self.gate.transition(request_id, "approved", "executing", "executing", "executing approved action")
        self.gate.execute_one(record)
        self.assertEqual(self.gate.load(request_id)["state"], "failed")
        self.assertEqual(self.route.executions, 0)

    def test_restart_barrier_runs_even_when_body_cleanup_fails(self):
        request_id = self.submit()["id"]
        self.gate.decide(request_id, "approve")
        record = self.gate.transition(request_id, "approved", "executing", "executing", "executing approved action")
        self.route.result = ActionResult(
            "uncertain", "writer_unstopped", "writer shutdown is unproven", restart_before_reconcile=True
        )
        exits = []
        self.gate.fatal_exit = lambda: exits.append(True)
        with mock.patch.object(self.gate, "_delete_body", side_effect=OSError("unlink failed")):
            with self.assertRaises(OSError):
                self.gate.execute_one(record)
        self.assertEqual(exits, [True])
        self.assertTrue(self.gate.stopping.is_set())
        self.assertEqual(self.gate.load(request_id)["state"], "uncertain")
        self.assertEqual(self.route.reconciliations, 0)

    def test_publication_failure_leaves_only_complete_or_absent_request(self):
        real_fsync = core.fsync_dir

        def fail_parent_fsync(path):
            if path == self.gate.requests_dir:
                raise OSError("injected parent fsync failure")
            return real_fsync(path)

        with mock.patch("external_gate.gate.fsync_dir", side_effect=fail_parent_fsync):
            with self.assertRaises(GateError):
                self.submit(value="crash")
        self.assertFalse(any(entry.name.startswith(".submit-") for entry in self.gate.requests_dir.iterdir()))
        records = [entry for entry in self.gate.requests_dir.iterdir() if entry.name and not entry.name.startswith(".")]
        self.assertLessEqual(len(records), 1)
        if records:
            self.assertTrue((records[0] / "body").is_file())
            self.assertTrue((records[0] / "request.json").is_file())
            Gate(
                self.config, {self.route.name: FakeRoute()},
                wall_clock=self.clock, monotonic=self.monotonic, fatal_exit=lambda: None,
            )

    def test_terminal_body_cleanup_failure_stops_gate_and_admission(self):
        request_id = self.submit(value="cleanup")["id"]
        exits = []
        self.gate.fatal_exit = lambda: exits.append(True)
        with mock.patch.object(self.gate, "_delete_body", side_effect=OSError("unlink failed")):
            with self.assertRaises(OSError):
                self.gate.transition(request_id, "pending", "denied", "denied", "denied")
        self.assertEqual(exits, [True])
        self.assertEqual(self.gate.load(request_id)["state"], "denied")
        self.assertFalse(self.gate.healthy())
        with self.assertRaises(GateError) as raised:
            self.submit(value="blocked")
        self.assertEqual(raised.exception.code, "gate_stopping")

    def test_crash_after_executing_replace_never_reverts_to_approved(self):
        request_id = self.submit()["id"]
        self.gate.decide(request_id, "approve")
        request_dir = self.gate.request_path(request_id)
        real_fsync = core.fsync_dir

        def crash_after_replace(path):
            if path == request_dir:
                raise OSError("injected crash")
            return real_fsync(path)

        with mock.patch("external_gate.gate.fsync_dir", side_effect=crash_after_replace):
            with self.assertRaises(OSError):
                self.gate.transition(request_id, "approved", "executing", "executing", "executing approved action")
        self.assertEqual(self.gate.load(request_id)["state"], "executing")
        replacement = Gate(
            self.config,
            {self.route.name: FakeRoute()},
            wall_clock=self.clock,
            monotonic=self.monotonic,
            fatal_exit=lambda: None,
        )
        self.assertEqual(replacement.load(request_id)["state"], "uncertain")

    def test_every_state_transition_survives_crash_after_replace(self):
        transitions = (
            ("pending", "approved"), ("pending", "denied"), ("pending", "expired"),
            ("approved", "executing"),
            ("executing", "succeeded"), ("executing", "failed"), ("executing", "uncertain"),
            ("uncertain", "succeeded"), ("uncertain", "failed"), ("uncertain", "abandoned"),
        )
        for index, (source, target) in enumerate(transitions):
            with self.subTest(source=source, target=target):
                config = GateConfig(
                    state_dir=self.root / f"crash-{index}",
                    socket_path=self.root / f"crash-socket-{index}" / "request.sock",
                    public_origin=self.config.public_origin,
                    password=self.config.password,
                )
                gate = Gate(
                    config, {self.route.name: FakeRoute()},
                    wall_clock=self.clock, monotonic=self.monotonic, fatal_exit=lambda: None,
                )
                request_id = self.submit(value=f"crash-{index}", gate=gate)["id"]
                if source in {"approved", "executing", "uncertain"}:
                    gate.transition(request_id, "pending", "approved", "approved", "approved")
                if source in {"executing", "uncertain"}:
                    gate.transition(request_id, "approved", "executing", "executing", "executing")
                if source == "uncertain":
                    gate.transition(request_id, "executing", "uncertain", "unknown", "unknown")
                request_dir = gate.request_path(request_id)
                real_fsync = core.fsync_dir

                def crash(path):
                    if path == request_dir:
                        raise OSError("injected transition crash")
                    return real_fsync(path)

                with mock.patch("external_gate.gate.fsync_dir", side_effect=crash):
                    with self.assertRaises(OSError):
                        gate.transition(request_id, source, target, "crash_test", "crash test")
                self.assertEqual(gate.load(request_id)["state"], target)
                Gate(
                    config, {self.route.name: FakeRoute()},
                    wall_clock=self.clock, monotonic=self.monotonic, fatal_exit=lambda: None,
                )

    def test_admission_status_and_worker_storage_failures_stop_gate(self):
        for operation in ("admission", "status", "worker"):
            with self.subTest(operation=operation):
                config = GateConfig(
                    state_dir=self.root / f"storage-{operation}",
                    socket_path=self.root / f"storage-socket-{operation}" / "request.sock",
                    public_origin=self.config.public_origin,
                    password=self.config.password,
                )
                exits = []
                gate = Gate(
                    config, {self.route.name: FakeRoute()},
                    wall_clock=self.clock, monotonic=time.monotonic,
                    fatal_exit=lambda: exits.append(True),
                )
                if operation == "admission":
                    with mock.patch.object(gate, "_records", side_effect=OSError("storage read failed")):
                        with self.assertRaises(GateError):
                            self.submit(gate=gate)
                elif operation == "status":
                    request_id = self.submit(gate=gate)["id"]
                    with mock.patch.object(gate, "load", side_effect=OSError("storage read failed")):
                        self.assertIsNone(gate.status(request_id))
                else:
                    with mock.patch.object(gate, "_next_approved", side_effect=OSError("storage read failed")):
                        gate.start_worker()
                        gate.worker.join(timeout=1)
                self.assertEqual(exits, [True])
                self.assertFalse(gate.healthy())

    def test_body_byte_quota_includes_reservations(self):
        old_limit = core.MAX_OPEN_BODY_BYTES
        core.MAX_OPEN_BODY_BYTES = 10
        try:
            self.submit(body=b"123456")
            with self.assertRaises(GateError) as raised:
                self.submit(body=b"12345")
            self.assertEqual(raised.exception.code, "body_quota")
            self.gate.reserved_bytes = 5
            with self.assertRaises(GateError):
                self.submit(body=b"x")
            self.gate.reserved_bytes = 0
        finally:
            core.MAX_OPEN_BODY_BYTES = old_limit

    def test_persisted_approval_is_the_queue(self):
        request_id = self.submit()["id"]
        self.assertTrue(self.gate.decide(request_id, "approve"))
        replacement_route = FakeRoute()
        replacement = Gate(
            self.config,
            {replacement_route.name: replacement_route},
            wall_clock=self.clock,
            monotonic=time.monotonic,
            fatal_exit=lambda: None,
        )
        try:
            replacement.start_worker()
            self.wait_state(request_id, "succeeded", replacement)
            self.assertEqual(replacement_route.executions, 1)
        finally:
            replacement.stop_worker()

    def test_executing_is_reconciled_after_restart_not_executed(self):
        request_id = self.submit()["id"]
        self.gate.decide(request_id, "approve")
        self.gate.transition(request_id, "approved", "executing", "executing", "executing approved action")
        route = FakeRoute()
        replacement = Gate(
            self.config,
            {route.name: route},
            wall_clock=self.clock,
            monotonic=self.monotonic,
            fatal_exit=lambda: None,
        )
        replacement.maintenance()
        self.assertEqual(replacement.load(request_id)["state"], "failed")
        self.assertEqual(route.executions, 0)
        self.assertEqual(route.reconciliations, 1)

    def test_maintenance_reconciles_two_then_abandons_old_uncertainty(self):
        ids = []
        for index in range(3):
            request_id = self.submit(value=str(index))["id"]
            self.gate.decide(request_id, "approve")
            self.gate.transition(request_id, "approved", "executing", "executing", "executing approved action")
            self.gate.transition(request_id, "executing", "uncertain", "unknown", "outcome unknown")
            ids.append(request_id)
        self.route.reconcile_result = ActionResult("uncertain", "remote_unavailable", "remote unavailable")
        self.gate.maintenance()
        self.assertEqual(self.route.reconciliations, 2)
        self.clock.value += 100
        self.gate.maintenance()
        self.assertTrue(all(self.gate.load(request_id)["state"] == "abandoned" for request_id in ids))

    def test_startup_cleans_temporary_and_nonexecuting_bodies(self):
        denied = self.submit(value="denied")["id"]
        self.gate.decide(denied, "deny")
        uncertain = self.submit(value="uncertain")["id"]
        self.gate.decide(uncertain, "approve")
        self.gate.transition(uncertain, "approved", "executing", "executing", "executing approved action")
        self.gate.transition(uncertain, "executing", "uncertain", "unknown", "unknown")
        abandoned = self.submit(value="abandoned")["id"]
        self.gate.decide(abandoned, "approve")
        self.gate.transition(abandoned, "approved", "executing", "executing", "executing approved action")
        self.gate.transition(abandoned, "executing", "uncertain", "unknown", "unknown")
        self.gate.transition(abandoned, "uncertain", "abandoned", "abandoned", "inspect manually")
        bodies = [self.gate.request_path(request_id) / "body" for request_id in (denied, uncertain, abandoned)]
        for body in bodies:
            body.write_bytes(b"leftover")
        temporary = self.gate.requests_dir / ".submit-abandoned"
        temporary.mkdir()
        (temporary / "body").write_bytes(b"partial")
        nested_temporary = self.gate.request_path(denied) / ".request.json.crash.tmp"
        nested_temporary.write_text("partial")
        Gate(self.config, {self.route.name: FakeRoute()}, wall_clock=self.clock, monotonic=self.monotonic, fatal_exit=lambda: None)
        self.assertTrue(all(not body.exists() for body in bodies))
        self.assertFalse(temporary.exists())
        self.assertFalse(nested_temporary.exists())

    def test_decode_failure_fails_pending_and_abandons_uncertain(self):
        pending = self.submit(value="pending")["id"]
        uncertain = self.submit(value="uncertain")["id"]
        self.gate.decide(uncertain, "approve")
        self.gate.transition(uncertain, "approved", "executing", "executing", "executing approved action")
        self.gate.transition(uncertain, "executing", "uncertain", "unknown", "unknown")
        for request_id in (pending, uncertain):
            record = self.gate.load(request_id)
            record["action"] = {"broken": True}
            self.gate._write_record(record)
        self.assertIsNone(self.gate.approval_fields(pending))
        self.assertEqual(self.gate.load(pending)["state"], "failed")
        self.assertIsNone(self.gate.approval_fields(uncertain))
        self.assertEqual(self.gate.load(uncertain)["state"], "abandoned")

    def test_transition_logs_exclude_agent_controlled_values(self):
        secret = "hostile-secret-value"
        request_id = self.submit(value=secret)["id"]
        with self.assertLogs("external_gate", level="INFO") as captured:
            self.gate.transition(request_id, "pending", "denied", "denied", "denied")
        output = "\n".join(captured.output)
        self.assertNotIn(secret, output)
        self.assertNotIn(str(self.gate.request_path(request_id)), output)
        self.assertIn(f"id={request_id}", output)
        self.assertIn("code=denied", output)

    def test_terminal_pruning_runs_on_transition(self):
        config = GateConfig(
            state_dir=self.root / "prune-state",
            socket_path=self.root / "prune-socket" / "request.sock",
            public_origin=self.config.public_origin,
            password=self.config.password,
            max_terminal_records=1,
        )
        gate = Gate(config, {self.route.name: FakeRoute()}, wall_clock=self.clock, monotonic=self.monotonic, fatal_exit=lambda: None)
        first = self.submit(gate=gate)["id"]
        gate.decide(first, "deny")
        self.clock.value += 1
        second = self.submit(gate=gate)["id"]
        gate.decide(second, "deny")
        self.assertFalse(gate.request_path(first).exists())
        self.assertTrue(gate.request_path(second).exists())

    def test_action_description_result_and_record_limits(self):
        with self.assertRaises(GateError):
            self.submit(value="x" * (core.MAX_VALUE_BYTES + 1))
        original_describe = self.route.describe
        self.route.describe = lambda _action: [("field", "value")] * (core.MAX_DESCRIPTION_FIELDS + 1)
        with self.assertRaises(GateError):
            self.submit()
        self.route.describe = lambda _action: [("x" * (core.MAX_LABEL_BYTES + 1), "value")]
        with self.assertRaises(GateError):
            self.submit()
        self.route.describe = original_describe
        request_id = self.submit(value="result")["id"]
        self.gate.transition(request_id, "pending", "denied", "denied", "x" * 600)
        record = self.gate.load(request_id)
        self.assertEqual(len(record["result"]["message"].encode()), core.MAX_RESULT_BYTES)
        with self.assertRaises(ValueError):
            self.gate._validate_result(ActionResult("failed", "too_large", "x" * (core.MAX_RESULT_BYTES + 1)))
        self.route.freeze = lambda *_args: {"value": "x" * core.MAX_ACTION_BYTES, "digest": "d", "size": 1}
        with self.assertRaises(GateError):
            self.submit()

    def test_open_quota_counts_uncertain_and_abandoned_releases_it(self):
        old_limit = core.MAX_OPEN_REQUESTS
        core.MAX_OPEN_REQUESTS = 1
        try:
            first = self.submit()["id"]
            self.gate.decide(first, "approve")
            self.gate.transition(first, "approved", "executing", "executing", "executing approved action")
            self.gate.transition(first, "executing", "uncertain", "unknown", "unknown")
            with self.assertRaises(GateError):
                self.submit(value="blocked")
            self.gate.transition(first, "uncertain", "abandoned", "abandoned", "inspect manually")
            self.submit(value="accepted")
        finally:
            core.MAX_OPEN_REQUESTS = old_limit

    def test_each_open_state_holds_a_request_slot(self):
        old_limit = core.MAX_OPEN_REQUESTS
        core.MAX_OPEN_REQUESTS = 1
        try:
            for state in ("pending", "approved", "executing", "uncertain"):
                with self.subTest(state=state):
                    config = GateConfig(
                        state_dir=self.root / f"quota-{state}",
                        socket_path=self.root / f"socket-{state}" / "request.sock",
                        public_origin=self.config.public_origin,
                        password=self.config.password,
                    )
                    gate = Gate(
                        config, {self.route.name: FakeRoute()},
                        wall_clock=self.clock, monotonic=self.monotonic, fatal_exit=lambda: None,
                    )
                    request_id = self.submit(gate=gate)["id"]
                    if state != "pending":
                        gate.decide(request_id, "approve")
                    if state in {"executing", "uncertain"}:
                        gate.transition(request_id, "approved", "executing", "executing", "executing approved action")
                    if state == "uncertain":
                        gate.transition(request_id, "executing", "uncertain", "unknown", "unknown")
                    with self.assertRaises(GateError):
                        self.submit(value="blocked", gate=gate)
        finally:
            core.MAX_OPEN_REQUESTS = old_limit

    def start_unix_server(self):
        AgentHandler.gate = self.gate
        with contextlib.suppress(FileNotFoundError):
            self.config.socket_path.unlink()
        server = UnixHTTPServer(str(self.config.socket_path), AgentHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def raw_unix(self, request: bytes, timeout=1):
        client = socket.socket(socket.AF_UNIX)
        client.settimeout(timeout)
        client.connect(str(self.config.socket_path))
        client.sendall(request)
        client.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            try:
                chunk = client.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
        client.close()
        return b"".join(chunks)

    def test_agent_http_rejects_unknown_route_framing_and_metadata(self):
        server, _thread = self.start_unix_server()
        try:
            unknown = self.raw_unix(b"POST /v1/requests/nope.v1 HTTP/1.1\r\nContent-Length: 1\r\n\r\nx")
            self.assertIn(b"404", unknown.split(b"\r\n", 1)[0])
            duplicate = self.raw_unix(
                b"POST /v1/requests/fake.v1 HTTP/1.1\r\nContent-Type: application/octet-stream\r\n"
                b"Content-Length: 1\r\nContent-Length: 1\r\nX-Vivarium-Value: x\r\n\r\nx"
            )
            self.assertIn(b"400", duplicate.split(b"\r\n", 1)[0])
            transfer = self.raw_unix(
                b"POST /v1/requests/fake.v1 HTTP/1.1\r\nContent-Type: application/octet-stream\r\n"
                b"Transfer-Encoding: chunked\r\nX-Vivarium-Value: x\r\n\r\n1\r\nx\r\n0\r\n\r\n"
            )
            self.assertIn(b"400", transfer.split(b"\r\n", 1)[0])
            extra = self.raw_unix(
                b"POST /v1/requests/fake.v1 HTTP/1.1\r\nContent-Type: application/octet-stream\r\n"
                b"Content-Length: 1\r\nX-Vivarium-Value: x\r\nX-Vivarium-Other: y\r\n\r\nx"
            )
            self.assertIn(b"400", extra.split(b"\r\n", 1)[0])
        finally:
            server.shutdown()
            server.server_close()

    def test_one_unix_reader_blocks_a_second_upload(self):
        server, _thread = self.start_unix_server()
        first = socket.socket(socket.AF_UNIX)
        second = socket.socket(socket.AF_UNIX)
        try:
            first.connect(str(self.config.socket_path))
            first.sendall(
                b"POST /v1/requests/fake.v1 HTTP/1.1\r\nContent-Type: application/octet-stream\r\n"
                b"Content-Length: 4\r\nX-Vivarium-Value: first\r\n\r\nx"
            )
            second.settimeout(0.15)
            second.connect(str(self.config.socket_path))
            second.sendall(
                b"POST /v1/requests/fake.v1 HTTP/1.1\r\nContent-Type: application/octet-stream\r\n"
                b"Content-Length: 1\r\nX-Vivarium-Value: second\r\n\r\ny"
            )
            with self.assertRaises(socket.timeout):
                second.recv(1)
            first.close()
            second.settimeout(2)
            self.assertIn(b"201", second.recv(4096).split(b"\r\n", 1)[0])
        finally:
            first.close()
            second.close()
            server.shutdown()
            server.server_close()

    def test_browser_auth_csrf_and_origin(self):
        request_id = self.submit()["id"]
        BrowserHandler.gate = self.gate
        server = BrowserHTTPServer(("127.0.0.1", 0), BrowserHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        auth = "Basic " + base64.b64encode(("vivarium:" + self.config.password).encode()).decode()
        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(base + f"/r/{request_id}")
            self.assertEqual(denied.exception.code, 401)
            page_request = urllib.request.Request(base + f"/r/{request_id}", headers={"Authorization": auth})
            page = urllib.request.urlopen(page_request).read().decode()
            self.assertIn("Bundle SHA-256", page)
            form = urlencode({"csrf": self.gate.csrf_token}).encode()
            wrong_origin = urllib.request.Request(
                base + f"/r/{request_id}/approve",
                data=form,
                method="POST",
                headers={
                    "Authorization": auth,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "http://wrong.invalid",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as forbidden:
                urllib.request.urlopen(wrong_origin)
            self.assertEqual(forbidden.exception.code, 403)
            approved = urllib.request.Request(
                base + f"/r/{request_id}/approve",
                data=form,
                method="POST",
                headers={"Authorization": auth, "Content-Type": "application/x-www-form-urlencoded"},
            )
            urllib.request.urlopen(approved)
            self.assertEqual(self.gate.load(request_id)["state"], "approved")
        finally:
            server.shutdown()
            server.server_close()


class ExternalGateConfigTests(unittest.TestCase):
    def values(self, mode="loopback", bind="127.0.0.1", public="http://127.0.0.1:7843"):
        return {
            "EXTERNAL_GATE_ENABLE": "true",
            "EXTERNAL_GATE_APPROVAL_PASSWORD": "a" * 32,
            "EXTERNAL_GATE_APPROVAL_MODE": mode,
            "EXTERNAL_GATE_APPROVAL_BIND_ADDR": bind,
            "EXTERNAL_GATE_PUBLIC_URL": public,
            "EXTERNAL_GATE_SSH_KEY_FINGERPRINT": "SHA256:" + "a" * 43,
        }

    def test_transport_tuple_allowlist(self):
        self.assertEqual(validate_transport(self.values()), "http://127.0.0.1:7843")
        self.assertEqual(
            validate_transport(self.values("proxy", "127.0.0.1", "https://gate.example")),
            "https://gate.example",
        )
        self.assertEqual(
            validate_transport(self.values("tailscale", "100.64.0.2", "http://100.64.0.2:7843")),
            "http://100.64.0.2:7843",
        )
        rejected = [
            self.values("loopback", "127.0.0.1", "http://localhost:7843"),
            self.values("proxy", "0.0.0.0", "https://gate.example"),
            self.values("tailscale", "100.64.0.2", "http://100.64.0.3:7843"),
            self.values("tailscale", "100.64.0.2", "https://100.64.0.2:7843"),
            self.values("loopback", "127.0.0.1", "http://127.0.0.1:7843/path"),
        ]
        for values in rejected:
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_transport(values)

    def test_config_parser_requires_exact_unique_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gate.env"
            values = self.values()
            path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
            self.assertEqual(read_config(path), values)
            path.write_text(path.read_text() + "UNKNOWN=value\n")
            with self.assertRaises(ValueError):
                read_config(path)
            path.write_text("\n".join(f"{key}={value}" for key, value in values.items() if key != next(iter(CONFIG_KEYS))))
            with self.assertRaises(ValueError):
                read_config(path)

    def test_losing_daemon_lock_owner_cannot_touch_socket(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir()
            socket_marker = Path(temporary) / "request.sock"
            socket_marker.write_text("owner")
            owner_fd = acquire_daemon_lock(state)
            try:
                with self.assertRaises(BlockingIOError):
                    acquire_daemon_lock(state)
                self.assertEqual(socket_marker.read_text(), "owner")
            finally:
                fcntl.flock(owner_fd, fcntl.LOCK_UN)
                os.close(owner_fd)

    def test_container_files_preserve_hardening_and_bounds(self):
        root = Path(__file__).parents[1]
        dockerfile = (root / "Dockerfile.external-gate").read_text()
        compose = (root / "compose.external-gate.yaml").read_text()
        client = (root / "compose.external-gate-client.yaml").read_text()
        self.assertRegex(dockerfile.splitlines()[0], r"^FROM .+@sha256:[0-9a-f]{64}$")
        self.assertIn("USER vivarium", dockerfile)
        self.assertIn('ENTRYPOINT ["python", "-m", "external_gate"]', dockerfile)
        for expected in (
            'name: vivarium-external-gate', 'cap_drop: ["ALL"]', 'no-new-privileges:true',
            'read_only: true', 'cpus: 1.0', 'mem_limit: 2g', 'pids_limit: 128',
            'size=1073741824', 'max-size: 10m', 'max-file: "3"',
        ):
            self.assertIn(expected, compose)
        self.assertNotIn("cap_add", compose)
        self.assertNotIn("privileged", compose)
        self.assertNotIn("/var/run/docker.sock", compose)
        self.assertNotIn("vivarium-home", compose)
        self.assertEqual(client.count("source:"), 1)
        self.assertIn("VIVARIUM_EXTERNAL_GATE_SOCKET", client)
        self.assertNotIn("external-gate-ssh", client)
        self.assertNotIn("external-gate.env", client)


class ExternalGateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parents[1]

    def test_vpush_uses_only_the_new_typed_endpoint_and_flow(self):
        script = (self.root / "scripts" / "vpush").read_text()
        self.assertIn("/run/vivarium-external-gate/request.sock", script)
        self.assertIn("/v1/requests/git.push-branch.v1", script)
        self.assertIn("request submitted; no push has happened yet", script)
        self.assertEqual(script.count("X-Vivarium-"), 6)
        for legacy in ("VIVARIUM_PUSH_GATE_SOCKET", "/run/vivarium-push-gate", "http://localhost/requests"):
            self.assertNotIn(legacy, script)
        result = subprocess.run(
            [str(self.root / "scripts" / "vpush")],
            env={"PATH": "/usr/bin:/bin", "VIVARIUM_EXTERNAL_GATE_SOCKET": "/missing/socket"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("external gate is unavailable", result.stderr)

    def test_vpush_rejects_non_ascii_branch_before_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.email", "test@example.invalid"], check=True)
            (repository / "file").write_text("one\n")
            subprocess.run(["git", "-C", str(repository), "add", "file"], check=True)
            subprocess.run(["git", "-C", str(repository), "commit", "-qm", "one"], check=True)
            subprocess.run(["git", "-C", str(repository), "checkout", "-qb", "café"], check=True)
            socket_path = root / "request.sock"
            marker = socket.socket(socket.AF_UNIX)
            marker.bind(str(socket_path))
            try:
                result = subprocess.run(
                    [str(self.root / "scripts" / "vpush")],
                    cwd=repository,
                    env={
                        "PATH": "/usr/bin:/bin",
                        "VIVARIUM_EXTERNAL_GATE_SOCKET": str(socket_path),
                    },
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            finally:
                marker.close()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid branch", result.stderr)

    def test_profile_lifecycle_uses_one_shared_socket_overlay(self):
        profile = (self.root / "scripts" / "profile.sh").read_text()
        up = (self.root / "scripts" / "up.sh").read_text()
        rebuild = (self.root / "scripts" / "rebuild.sh").read_text()
        overlay = (self.root / "compose.external-gate-client.yaml").read_text()
        self.assertIn("$HOME/.local/share/vivarium-external-gate", profile)
        self.assertIn("compose.external-gate-client.yaml", profile)
        self.assertIn("./scripts/external-gate.sh start", up)
        self.assertIn("./scripts/external-gate.sh start", rebuild)
        self.assertEqual(overlay.count("source:"), 1)
        self.assertNotIn("depends_on", overlay)

    def test_clean_cutover_has_no_legacy_implementation(self):
        for relative in (
            "scripts/push-gate.sh", "scripts/push-gate-broker.py",
            "compose.push-gate.yaml", "tests/test_push_gate.py",
        ):
            self.assertFalse((self.root / relative).exists())
        profile_create = (self.root / "scripts" / "profile-create.sh").read_text()
        self.assertIn("./scripts/external-gate.sh enable", profile_create)
        remove = (self.root / "scripts" / "remove.sh").read_text()
        self.assertIn("$REMOVE_REPO", remove)
        self.assertIn("external-gate.sh' stop", remove)

    def test_profile_resolution_fails_closed_on_malformed_enable_setting(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".config" / "vivarium" / "external-gate.env"
            config.parent.mkdir(parents=True)
            config.write_text(" EXTERNAL_GATE_ENABLE=true\n")
            config.chmod(0o600)
            profile_env = home / "work.env"
            profile_env.write_text("")
            result = subprocess.run(
                ["bash", "-c", '. ./scripts/profile.sh "$1"', "profile-test", str(profile_env)],
                cwd=self.root,
                env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("[FATAL]", result.stderr)
            self.assertIn("external-gate.sh disable", result.stderr)

    def test_stop_fails_instead_of_claiming_success_without_docker(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [str(self.root / "scripts" / "external-gate.sh"), "stop"],
                cwd=self.root,
                env={"HOME": temporary, "PATH": "/usr/bin:/bin"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("verify that the external gate stopped", result.stderr)
            self.assertNotIn("[external-gate] stopped", result.stdout)

    def test_disable_stops_even_with_malformed_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".config" / "vivarium" / "external-gate.env"
            config.parent.mkdir(parents=True)
            config.write_text("EXTERNAL_GATE_ENABLE=true\nBROKEN=value\n")
            config.chmod(0o600)
            fake_bin = home / "bin"
            fake_bin.mkdir()
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\n"
                "case \"${1:-}\" in\n"
                "  info) exit 0 ;;\n"
                "  container) [[ \"${2:-}\" == inspect ]] && exit 1; exit 0 ;;\n"
                "  compose) [[ \"${2:-}\" == version ]] && exit 0; exit 0 ;;\n"
                "esac\n"
            )
            docker.chmod(0o755)
            result = subprocess.run(
                [str(self.root / "scripts" / "external-gate.sh"), "disable"],
                cwd=self.root,
                env={"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(config.exists())
            self.assertTrue((config.parent / "external-gate.env.invalid").is_file())
            self.assertIn("disabled", result.stdout)

    def test_lifecycle_commands_are_serialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            fake_bin = home / "bin"
            fake_bin.mkdir()
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\n"
                "case \"${1:-}\" in\n"
                "  info) sleep 0.2; exit 0 ;;\n"
                "  container) [[ \"${2:-}\" == inspect ]] && exit 1; exit 0 ;;\n"
                "  compose) exit 0 ;;\n"
                "esac\n"
            )
            docker.chmod(0o755)
            env = {"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"}
            started = time.monotonic()
            first = subprocess.Popen(
                [str(self.root / "scripts" / "external-gate.sh"), "stop"],
                cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            second = subprocess.Popen(
                [str(self.root / "scripts" / "external-gate.sh"), "stop"],
                cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            first.communicate(timeout=3)
            second.communicate(timeout=3)
            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 0)
            self.assertGreaterEqual(time.monotonic() - started, 0.35)

    def test_concurrent_starts_create_one_container_and_config_change_recreates(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".config" / "vivarium" / "external-gate.env"
            config.parent.mkdir(parents=True)
            values = ExternalGateConfigTests().values()
            values["EXTERNAL_GATE_SSH_KEY_FINGERPRINT"] = "SHA256:test"
            config.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
            config.chmod(0o600)
            agent_socket = home / "agent.sock"
            agent = socket.socket(socket.AF_UNIX)
            agent.bind(str(agent_socket))
            fake_bin = home / "bin"
            fake_bin.mkdir()
            marker = home / "container.marker"
            log = home / "docker.log"
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"${1:-}\" == compose && \"${2:-}\" == version ]]; then exit 0; fi\n"
                "if [[ \"${1:-}\" == compose ]]; then\n"
                "  echo \"$*\" >> \"$FAKE_LOG\"\n"
                "  if [[ \" $* \" == *\" up \"* && ! -e \"$FAKE_MARKER\" ]]; then touch \"$FAKE_MARKER\"; echo create >> \"$FAKE_LOG\"; fi\n"
                "  sleep 0.15\n"
                "  exit 0\n"
                "fi\n"
                "exit 0\n"
            )
            docker.chmod(0o755)
            ssh_add = fake_bin / "ssh-add"
            ssh_add.write_text("#!/usr/bin/env bash\necho '256 SHA256:test key (ED25519)'\n")
            ssh_add.chmod(0o755)
            curl = fake_bin / "curl"
            curl.write_text("#!/usr/bin/env bash\nexit 0\n")
            curl.chmod(0o755)
            env = {
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "SSH_AUTH_SOCK": str(agent_socket),
                "FAKE_LOG": str(log),
                "FAKE_MARKER": str(marker),
            }
            try:
                processes = [
                    subprocess.Popen(
                        [str(self.root / "scripts" / "external-gate.sh"), "start"],
                        cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    for _ in range(2)
                ]
                for process in processes:
                    _stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertEqual(log.read_text().splitlines().count("create"), 1)
                config.write_text(config.read_text().replace("a" * 32, "b" * 32))
                config.chmod(0o600)
                changed = subprocess.run(
                    [str(self.root / "scripts" / "external-gate.sh"), "start"],
                    cwd=self.root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                self.assertEqual(changed.returncode, 0, changed.stderr)
                self.assertIn("--force-recreate", log.read_text().splitlines()[-1])
            finally:
                agent.close()

    def test_remove_everything_stops_gate_and_preserves_gate_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            installation = home / "vivarium"
            scripts = installation / "scripts"
            scripts.mkdir(parents=True)
            for relative in ("scripts/remove.sh", "scripts/profile.sh", "scripts/external-gate.sh"):
                shutil.copy2(self.root / relative, installation / relative)
            shutil.copy2(self.root / "compose.external-gate.yaml", installation / "compose.external-gate.yaml")
            gate_state = home / ".local" / "state" / "vivarium-external-gate"
            gate_state.mkdir(parents=True)
            (gate_state / "keep").write_text("state")
            gate_config = home / ".config" / "vivarium" / "external-gate.env"
            gate_config.parent.mkdir(parents=True)
            preserved_config = ExternalGateConfigTests().values()
            preserved_config["EXTERNAL_GATE_ENABLE"] = "false"
            config_text = "\n".join(f"{key}={value}" for key, value in preserved_config.items()) + "\n"
            gate_config.write_text(config_text)
            gate_config.chmod(0o600)
            marker = home / "gate.container"
            marker.write_text("running")
            fake_bin = home / "bin"
            fake_bin.mkdir()
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"${1:-}\" == info ]]; then exit 0; fi\n"
                "if [[ \"${1:-}\" == container && \"${2:-}\" == inspect ]]; then [[ -e \"$GATE_MARKER\" ]]; exit $?; fi\n"
                "if [[ \"${1:-}\" == compose && \"${2:-}\" == version ]]; then exit 0; fi\n"
                "if [[ \"${1:-}\" == compose ]]; then rm -f \"$GATE_MARKER\"; exit 0; fi\n"
                "if [[ \"${1:-}\" == ps ]]; then exit 0; fi\n"
                "if [[ \"${1:-}\" == image ]]; then exit 1; fi\n"
                "exit 0\n"
            )
            docker.chmod(0o755)
            result = subprocess.run(
                [str(scripts / "remove.sh"), "--everything", "--yes"],
                cwd=installation,
                env={
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "GATE_MARKER": str(marker),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(marker.exists())
            self.assertFalse(installation.exists())
            self.assertEqual((gate_state / "keep").read_text(), "state")
            self.assertEqual(gate_config.read_text(), config_text)

    def test_lifecycle_rejects_bad_config_with_disable_instruction(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".config" / "vivarium" / "external-gate.env"
            config.parent.mkdir(parents=True)
            values = ExternalGateConfigTests().values(
                "tailscale", "100.64.0.2", "http://100.64.0.3:7843"
            )
            config.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
            config.chmod(0o600)
            result = subprocess.run(
                [str(self.root / "scripts" / "external-gate.sh"), "status"],
                cwd=self.root,
                env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("[FATAL]", result.stderr)
            self.assertIn("./scripts/external-gate.sh disable", result.stderr)


@unittest.skipUnless(shutil.which("docker"), "Docker is required for container integration")
class ExternalGateContainerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parents[1]
        self.temp = tempfile.TemporaryDirectory()
        self.host = Path(self.temp.name)
        self.image = f"vivarium-external-gate-test:{os.getpid()}"
        self.container = f"vivarium-external-gate-test-{os.getpid()}"
        self.agent_pid = None

    def tearDown(self):
        subprocess.run(["docker", "rm", "-f", self.container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "image", "rm", "-f", self.image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self.agent_pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.agent_pid, signal.SIGTERM)
        self.temp.cleanup()

    def test_compose_renders_only_reviewed_mounts_and_bounded_logs(self):
        state = self.host / "state"
        socket_dir = self.host / "socket"
        state.mkdir()
        socket_dir.mkdir()
        env = dict(
            os.environ,
            HOST_UID=str(os.getuid()),
            HOST_GID=str(os.getgid()),
            EXTERNAL_GATE_STATE_DIR=str(state),
            EXTERNAL_GATE_SOCKET_DIR=str(socket_dir),
            EXTERNAL_GATE_CONFIG_FILE="/dev/null",
            EXTERNAL_GATE_SSH_AUTH_SOCK="/dev/null",
            EXTERNAL_GATE_APPROVAL_BIND_ADDR="127.0.0.1",
        )
        rendered = subprocess.check_output(
            ["docker", "compose", "-f", "compose.external-gate.yaml", "-p", "vivarium-external-gate", "config"],
            cwd=self.root,
            env=env,
            text=True,
        )
        self.assertIn("cap_drop:\n      - ALL", rendered)
        self.assertNotIn("cap_add:", rendered)
        self.assertIn("read_only: true", rendered)
        self.assertIn("max-size: 10m", rendered)
        self.assertIn("vivarium-external-gate_default", rendered)
        self.assertNotIn("/var/run/docker.sock", rendered)
        self.assertNotIn("vivarium-home", rendered)

        profile_env = self.host / "profile.env"
        profile_env.write_text("")
        sources = []
        for profile in ("one", "two"):
            profile_rendered = subprocess.check_output(
                [
                    "docker", "compose", "-f", "compose.yaml", "-f", "compose.external-gate-client.yaml",
                    "--env-file", str(profile_env), "-p", f"vivarium-{profile}", "config",
                ],
                cwd=self.root,
                env=dict(env, VIVARIUM_PROFILE=profile, EXTERNAL_GATE_SOCKET_DIR=str(socket_dir)),
                text=True,
            )
            self.assertNotIn("external-gate-ssh", profile_rendered)
            self.assertNotIn("external-gate-config", profile_rendered)
            self.assertNotIn("vivarium-external-gate_default", profile_rendered)
            source_line = next(line.strip() for line in profile_rendered.splitlines() if str(socket_dir) in line)
            sources.append(source_line)
        self.assertEqual(sources[0], sources[1])

    def test_built_container_runtime_invariants_and_health(self):
        subprocess.run(
            [
                "docker", "build", "-f", "Dockerfile.external-gate",
                "--build-arg", f"UID={os.getuid()}", "--build-arg", f"GID={os.getgid()}",
                "-t", self.image, ".",
            ],
            cwd=self.root,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        image = json.loads(subprocess.check_output(["docker", "image", "inspect", self.image], text=True))[0]
        self.assertEqual(image["Config"]["User"], "vivarium")
        self.assertEqual(image["Config"]["Entrypoint"], ["python", "-m", "external_gate"])

        source = self.host / "large-source"
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
        (source / "large.bin").write_bytes(os.urandom(3 * 1024 * 1024))
        subprocess.run(["git", "-C", str(source), "add", "large.bin"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "large"], check=True)
        new_oid = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        bundle = self.host / "large.bundle"
        subprocess.run(["git", "-C", str(source), "bundle", "create", str(bundle), "HEAD"], check=True)
        check_code = f'''import json
from pathlib import Path
from external_gate.git_push import GitPushAction, GitPushRoute, ZERO_OID
route = GitPushRoute(Path("/scratch"), Path("/missing-agent"), "SHA256:test", Path("/opt/vivarium/external_gate/github_known_hosts"), agent_validator=lambda: None)
result = route.execute(GitPushAction("work", "example", "repo", "refs/heads/main", ZERO_OID, "{new_oid}"), Path("/input/request.bundle"))
print(json.dumps({{"state": result.state, "code": result.code}}))
'''
        exhausted = subprocess.check_output(
            [
                "docker", "run", "--rm", "--read-only", "--entrypoint", "python",
                "--tmpfs", f"/scratch:rw,noexec,nosuid,nodev,size=1m,mode=700,uid={os.getuid()},gid={os.getgid()}",
                "-v", f"{bundle}:/input/request.bundle:ro", self.image, "-c", check_code,
            ],
            text=True,
        )
        self.assertEqual(json.loads(exhausted), {"state": "failed", "code": "scratch_full"})

        agent_socket = self.host / "agent.sock"
        agent_output = subprocess.check_output(["ssh-agent", "-a", str(agent_socket), "-s"], text=True)
        self.agent_pid = int(re.search(r"SSH_AGENT_PID=([0-9]+)", agent_output).group(1))
        agent_env = dict(os.environ, SSH_AUTH_SOCK=str(agent_socket), SSH_AGENT_PID=str(self.agent_pid))
        empty_state = self.host / "empty-state"
        empty_socket_dir = self.host / "empty-socket"
        empty_state.mkdir(mode=0o700)
        empty_socket_dir.mkdir(mode=0o700)
        empty_config = self.host / "empty-agent.env"
        empty_values = ExternalGateConfigTests().values()
        empty_config.write_text("\n".join(f"{key}={value}" for key, value in empty_values.items()) + "\n")
        empty_config.chmod(0o600)
        empty_rejected = subprocess.run(
            [
                "docker", "run", "--rm", "--read-only",
                "--tmpfs", f"/var/tmp/vivarium-external-gate:rw,noexec,nosuid,nodev,size=8m,mode=700,uid={os.getuid()},gid={os.getgid()}",
                "-v", f"{empty_state}:/var/lib/vivarium-external-gate",
                "-v", f"{empty_socket_dir}:/run/vivarium-external-gate",
                "-v", f"{empty_config}:/run/vivarium-external-gate-config/external-gate.env:ro",
                "-v", f"{agent_socket}:/run/vivarium-external-gate-ssh/agent.sock:ro",
                self.image,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(empty_rejected.returncode, 0)
        self.assertIn("[FATAL] external gate startup failed", empty_rejected.stderr)
        self.assertNotIn("Traceback", empty_rejected.stderr)

        key = self.host / "test-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        subprocess.run(["ssh-add", str(key)], env=agent_env, check=True, stdout=subprocess.DEVNULL)
        identity = subprocess.check_output(["ssh-add", "-l"], env=agent_env, text=True)
        fingerprint = identity.split()[1]
        second_key = self.host / "second-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(second_key)], check=True)
        subprocess.run(["ssh-add", str(second_key)], env=agent_env, check=True, stdout=subprocess.DEVNULL)
        self.assertEqual(len(subprocess.check_output(["ssh-add", "-l"], env=agent_env, text=True).splitlines()), 2)

        state = self.host / "state"
        socket_dir = self.host / "socket"
        state.mkdir(mode=0o700)
        socket_dir.mkdir(mode=0o700)
        config = self.host / "external-gate.env"
        values = ExternalGateConfigTests().values()
        values["EXTERNAL_GATE_SSH_KEY_FINGERPRINT"] = fingerprint
        config.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
        config.chmod(0o600)

        bad_state = self.host / "bad-state"
        bad_socket_dir = self.host / "bad-socket"
        bad_state.mkdir(mode=0o700)
        bad_socket_dir.mkdir(mode=0o700)
        bad_config = self.host / "bad-external-gate.env"
        bad_values = dict(values, EXTERNAL_GATE_SSH_KEY_FINGERPRINT="SHA256:wrong-fingerprint")
        bad_config.write_text("\n".join(f"{key}={value}" for key, value in bad_values.items()) + "\n")
        bad_config.chmod(0o600)
        reject_command = [
            "docker", "run", "--rm", "--read-only",
            "--tmpfs", f"/var/tmp/vivarium-external-gate:rw,noexec,nosuid,nodev,size=8m,mode=700,uid={os.getuid()},gid={os.getgid()}",
            "-v", f"{bad_state}:/var/lib/vivarium-external-gate",
            "-v", f"{bad_socket_dir}:/run/vivarium-external-gate",
            "-v", f"{bad_config}:/run/vivarium-external-gate-config/external-gate.env:ro",
            "-v", f"{agent_socket}:/run/vivarium-external-gate-ssh/agent.sock:ro",
            self.image,
        ]
        multi_rejected = subprocess.run(
            reject_command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.assertNotEqual(multi_rejected.returncode, 0)
        self.assertNotIn("Traceback", multi_rejected.stderr)
        subprocess.run(["ssh-add", "-d", str(second_key) + ".pub"], env=agent_env, check=True, stdout=subprocess.DEVNULL)
        rejected = subprocess.run(
            reject_command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("[FATAL] external gate startup failed", rejected.stderr)
        self.assertNotIn("Traceback", rejected.stderr)
        self.assertNotIn(values["EXTERNAL_GATE_APPROVAL_PASSWORD"], rejected.stderr)

        for index, mode_values in enumerate((
            ExternalGateConfigTests().values("proxy", "127.0.0.1", "https://gate.example"),
            ExternalGateConfigTests().values("tailscale", "100.64.0.2", "http://100.64.0.2:7843"),
        )):
            mode_values["EXTERNAL_GATE_SSH_KEY_FINGERPRINT"] = fingerprint
            mode_state = self.host / f"mode-state-{index}"
            mode_socket = self.host / f"mode-socket-{index}"
            mode_state.mkdir(mode=0o700)
            mode_socket.mkdir(mode=0o700)
            mode_config = self.host / f"mode-{index}.env"
            mode_config.write_text("\n".join(f"{key}={value}" for key, value in mode_values.items()) + "\n")
            mode_config.chmod(0o600)
            mode_container = f"{self.container}-mode-{index}"
            try:
                subprocess.run(
                    [
                        "docker", "run", "-d", "--name", mode_container, "--read-only", "--cap-drop", "ALL",
                        "--tmpfs", f"/var/tmp/vivarium-external-gate:rw,noexec,nosuid,nodev,size=8m,mode=700,uid={os.getuid()},gid={os.getgid()}",
                        "-v", f"{mode_state}:/var/lib/vivarium-external-gate",
                        "-v", f"{mode_socket}:/run/vivarium-external-gate",
                        "-v", f"{mode_config}:/run/vivarium-external-gate-config/external-gate.env:ro",
                        "-v", f"{agent_socket}:/run/vivarium-external-gate-ssh/agent.sock:ro",
                        self.image,
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
                mode_request_socket = mode_socket / "request.sock"
                mode_deadline = time.monotonic() + 10
                while not mode_request_socket.is_socket() and time.monotonic() < mode_deadline:
                    time.sleep(0.1)
                self.assertTrue(mode_request_socket.is_socket())
                subprocess.run(
                    ["docker", "exec", mode_container, "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7843/healthz', timeout=2).read()"],
                    check=True,
                )
            finally:
                subprocess.run(["docker", "rm", "-f", mode_container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        subprocess.run(
            [
                "docker", "run", "-d", "--name", self.container,
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--memory", "2g", "--cpus", "1", "--pids-limit", "128",
                "--tmpfs", f"/var/tmp/vivarium-external-gate:rw,noexec,nosuid,nodev,size=1g,mode=700,uid={os.getuid()},gid={os.getgid()}",
                "-v", f"{state}:/var/lib/vivarium-external-gate",
                "-v", f"{socket_dir}:/run/vivarium-external-gate",
                "-v", f"{config}:/run/vivarium-external-gate-config/external-gate.env:ro",
                "-v", f"{agent_socket}:/run/vivarium-external-gate-ssh/agent.sock:ro",
                self.image,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        request_socket = socket_dir / "request.sock"
        deadline = time.monotonic() + 15
        while not request_socket.is_socket() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(request_socket.is_socket())
        subprocess.run(
            ["curl", "--silent", "--fail", "--unix-socket", str(request_socket), "http://localhost/healthz"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                "docker", "exec", self.container, "python", "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7843/healthz', timeout=2).read()",
            ],
            check=True,
        )
        hostile_profile = "hostile-secret-value"
        submitted = json.loads(subprocess.check_output(
            [
                "curl", "--silent", "--fail", "--unix-socket", str(request_socket),
                "-X", "POST", "-H", "Content-Type: application/x-git-bundle",
                "-H", f"X-Vivarium-Profile: {hostile_profile}",
                "-H", "X-Vivarium-Owner: example", "-H", "X-Vivarium-Repo: repo",
                "-H", "X-Vivarium-Ref: refs/heads/main", "-H", f"X-Vivarium-Old-Oid: {'0' * 40}",
                "-H", f"X-Vivarium-New-Oid: {new_oid}", "--data-binary", f"@{bundle}",
                "http://localhost/v1/requests/git.push-branch.v1",
            ],
            text=True,
        ))
        deny_code = f'''import base64, re, urllib.parse, urllib.request
request_id = "{submitted["id"]}"
auth = "Basic " + base64.b64encode(b"vivarium:{values["EXTERNAL_GATE_APPROVAL_PASSWORD"]}").decode()
page_request = urllib.request.Request("http://127.0.0.1:7843/r/" + request_id, headers={{"Authorization": auth}})
page = urllib.request.urlopen(page_request, timeout=2).read().decode()
token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
data = urllib.parse.urlencode({{"csrf": token}}).encode()
deny = urllib.request.Request("http://127.0.0.1:7843/r/" + request_id + "/deny", data=data, method="POST", headers={{"Authorization": auth, "Content-Type": "application/x-www-form-urlencoded", "Origin": "http://127.0.0.1:7843"}})
urllib.request.urlopen(deny, timeout=2).read()
'''
        subprocess.run(["docker", "exec", self.container, "python", "-c", deny_code], check=True)
        logs = subprocess.check_output(["docker", "logs", self.container], text=True, stderr=subprocess.STDOUT)
        self.assertIn(submitted["id"], logs)
        self.assertNotIn(hostile_profile, logs)
        self.assertNotIn(values["EXTERNAL_GATE_APPROVAL_PASSWORD"], logs)
        self.assertNotIn(str(bundle), logs)

        inspected = json.loads(subprocess.check_output(["docker", "inspect", self.container], text=True))[0]
        host_config = inspected["HostConfig"]
        self.assertTrue(host_config["ReadonlyRootfs"])
        self.assertFalse(host_config["Privileged"])
        self.assertEqual(host_config["CapDrop"], ["ALL"])
        self.assertFalse(host_config.get("CapAdd"))
        self.assertEqual(host_config["Memory"], 2 * 1024 * 1024 * 1024)
        self.assertEqual(host_config["NanoCpus"], 1_000_000_000)
        self.assertEqual(host_config["PidsLimit"], 128)
        self.assertEqual(len(host_config["Binds"]), 4)
        self.assertNotIn("/var/run/docker.sock", "\n".join(host_config["Binds"]))
        self.assertEqual(
            subprocess.check_output(["docker", "exec", self.container, "id", "-u"], text=True).strip(),
            str(os.getuid()),
        )


if __name__ == "__main__":
    unittest.main()
