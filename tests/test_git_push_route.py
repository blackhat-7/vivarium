import errno
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from external_gate.diff_viewer import MAX_FILE_BYTES, MAX_FILE_LINES, load_preview, render_preview
from external_gate.gate import ActionResult, FrozenSubmission, Gate, GateConfig, GateError
from external_gate.git_push import (
    AgentValidator,
    GitPushAction,
    GitPushRoute,
    NameStatusCollector,
    ProcessResult,
    ProcessRunner,
    RouteFailure,
    ZERO_OID,
    bounded_preview,
    valid_ref,
    validate_fields,
)


class LocalRoute(GitPushRoute):
    def __init__(self, scratch, remote):
        known_hosts = Path(__file__).parents[1] / "external_gate" / "github_known_hosts"
        super().__init__(
            scratch,
            Path("/tmp/test-agent.sock"),
            "SHA256:test",
            known_hosts,
            agent_validator=lambda: None,
        )
        self.remote = str(remote)

    def remote_url(self, _action):
        return self.remote

    def git_env(self):
        env = super().git_env()
        env.pop("GIT_SSH_COMMAND", None)
        env.pop("SSH_AUTH_SOCK", None)
        return env


class GitPushRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.remote = self.root / "remote.git"
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.email", "test@example.invalid"], check=True)
        self.route = LocalRoute(self.scratch, self.remote)
        self.ref = "refs/heads/feature"
        self.commit("one\n")

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, content):
        (self.source / "file").write_text(content)
        subprocess.run(["git", "-C", str(self.source), "add", "file"], check=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-qm", content.strip()], check=True)
        return subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()

    def bundle(self, name="request.bundle", *revisions):
        path = self.root / name
        subprocess.run(
            ["git", "-C", str(self.source), "bundle", "create", str(path), *(revisions or ("HEAD",))],
            check=True,
        )
        return path

    def action(self, old=ZERO_OID, new=None):
        new = new or subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        return GitPushAction("work", "example", "repo", self.ref, old, new)

    def remote_oid(self):
        result = subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-parse", "--verify", self.ref],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return result.stdout.strip() if result.returncode == 0 else ZERO_OID

    def test_preview_neutralizes_bidirectional_controls(self):
        preview, truncated = bounded_preview("safe\u202eevil\u2066text".encode(), 100)
        self.assertFalse(truncated)
        self.assertNotIn("\u202e", preview)
        self.assertNotIn("\u2066", preview)
        self.assertIn("\\u202E", preview)
        self.assertIn("\\u2066", preview)

    def test_name_status_stream_retains_three_hundred_and_counts_the_rest(self):
        payload = b"".join(f"M\0file-{index}\0".encode() for index in range(305))
        collector = NameStatusCollector()
        for index in range(0, len(payload), 7):
            collector(payload[index : index + 7])
        changed, extra = collector.finish()
        self.assertEqual((len(changed), extra), (300, 5))
        self.assertEqual((changed[0].path, changed[-1].path), ("file-0", "file-299"))

        rename = NameStatusCollector()
        rename(b"R100\0old\0new\0")
        changed, extra = rename.finish()
        self.assertEqual((changed[0].status, changed[0].old_path, changed[0].path, extra), ("renamed", "old", "new", 0))
        with self.assertRaises(ValueError):
            invalid = NameStatusCollector()
            invalid(b"M\0unterminated")
            invalid.finish()

    def test_authoritative_validation(self):
        validate_fields("work", "owner-1", "repo.name", "refs/heads/feature/x", ZERO_OID, "1" * 40)
        invalid = [
            ("work", "owner-", "repo", "refs/heads/main", ZERO_OID, "1" * 40),
            ("work", "owner", ".repo", "refs/heads/main", ZERO_OID, "1" * 40),
            ("work", "owner", "repo.git", "refs/heads/main", ZERO_OID, "1" * 40),
            ("work", "owner", "repo", "refs/tags/v1", ZERO_OID, "1" * 40),
            ("work", "owner", "repo", "refs/heads/-force", ZERO_OID, "1" * 40),
            ("work", "owner", "repo", "refs/heads/a/.hidden", ZERO_OID, "1" * 40),
            ("work", "owner", "repo", "refs/heads/main", ZERO_OID, ZERO_OID),
        ]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_fields(*fields)
        self.assertTrue(valid_ref("refs/heads/release/one"))
        self.assertFalse(valid_ref("refs/heads/a..b"))

    def test_freeze_declares_exact_action_with_bounded_diff_preview(self):
        new_oid = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        headers = dict(zip(self.route.metadata_headers, ["work", "example", "repo", self.ref, ZERO_OID, new_oid]))
        submission = self.route.freeze(headers, self.bundle(), "a" * 64, 10)
        self.assertIsInstance(submission, FrozenSubmission)
        self.assertEqual(
            set(submission.action),
            {
                "profile", "owner", "repo", "ref", "old_oid", "new_oid", "commit_count",
                "commits", "diff_stat",
            },
        )
        action = self.route.decode(submission.action)
        self.assertEqual(action.new_oid, new_oid)
        self.assertEqual(action.commit_count, 1)
        self.assertTrue(action.sidecar_preview)
        preview = load_preview(submission.preview.data)
        self.assertIn("+one", preview.files[0].patch)
        description = self.route.describe(action)
        self.assertEqual(
            [label for label, _value in description[:4]],
            ["Repository", "Branch", "Change", "Profile"],
        )
        self.assertEqual(description[0], ("Repository", "example/repo"))
        sections = self.route.approval_sections(action)
        self.assertEqual([section.kind for section in sections], ["code"])
        self.assertEqual(self.route.approval_diff(action).title, "Changes in this push")

    def test_gate_submission_persists_git_sidecar_with_multiline_stat(self):
        new_oid = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        bundle_path = self.bundle()
        body = bundle_path.read_bytes()
        offset = 0

        def read(amount):
            nonlocal offset
            chunk = body[offset : offset + amount]
            offset += len(chunk)
            return chunk

        gate = Gate(
            GateConfig(
                state_dir=self.root / "state",
                socket_path=self.root / "socket" / "gate.sock",
                public_origin="http://127.0.0.1:7843",
                password="a" * 32,
            ),
            {self.route.name: self.route},
            fatal_exit=lambda: None,
        )
        headers = dict(zip(self.route.metadata_headers, ["work", "example", "repo", self.ref, ZERO_OID, new_oid]))
        request_id = gate.submit(
            self.route.name,
            headers,
            len(body),
            read,
            gate.monotonic() + 10,
        )["id"]
        record, _fields, _sections, approval_diff, preview_data = gate.approval_fields(request_id)
        self.assertEqual(record["preview"]["kind"], "diff.v1")
        self.assertIsNotNone(approval_diff)
        self.assertIsNotNone(preview_data)

    def test_decode_preserves_existing_v1_actions_without_a_preview(self):
        new_oid = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        frozen = dict(zip(
            ("profile", "owner", "repo", "ref", "old_oid", "new_oid"),
            ("work", "example", "repo", self.ref, ZERO_OID, new_oid),
        ))
        action = self.route.decode(frozen)
        self.assertEqual(action.new_oid, new_oid)
        self.assertEqual(action.commit_count, 0)
        self.assertEqual(self.route.approval_sections(action), [])
        description = self.route.describe(action)
        self.assertEqual([label for label, _value in description[:4]], ["Repository", "Branch", "Change", "Profile"])
        self.assertEqual(description[2], ("Change", "Preview unavailable"))
        self.assertEqual([label for label, _value in description[4:]], ["Expected old commit", "Approved new commit"])

    def test_decode_preserves_existing_inline_preview_actions(self):
        new_oid = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        frozen = {
            "profile": "work",
            "owner": "example",
            "repo": "repo",
            "ref": self.ref,
            "old_oid": ZERO_OID,
            "new_oid": new_oid,
            "commit_count": 1,
            "commits": "abc1234\tone",
            "diff_stat": "1 file changed",
            "diff": "@@ -0,0 +1 @@\n+one",
            "diff_truncated": False,
        }
        action = self.route.decode(frozen)
        self.assertFalse(action.sidecar_preview)
        self.assertEqual([section.kind for section in self.route.approval_sections(action)], ["diff", "code"])
        self.assertIsNone(self.route.approval_diff(action))

    def test_oversized_file_is_omitted_without_hiding_a_later_file(self):
        old_oid = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        (self.source / "file").write_text("x" * (MAX_FILE_BYTES + 100) + "\n")
        (self.source / "many-lines.txt").write_text("line\n" * (MAX_FILE_LINES + 1))
        (self.source / "later.txt").write_text("still visible\n")
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "large and small"], check=True)
        new_oid = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        headers = dict(zip(self.route.metadata_headers, ["work", "example", "repo", self.ref, old_oid, new_oid]))
        submission = self.route.freeze(headers, self.bundle(), "a" * 64, 10)
        preview = load_preview(submission.preview.data)
        by_path = {file.path: file for file in preview.files}
        self.assertEqual(by_path["file"].omission_reason, "per_file_bytes")
        self.assertGreater(by_path["file"].additions + by_path["file"].deletions, 0)
        self.assertEqual(by_path["many-lines.txt"].omission_reason, "per_file_lines")
        self.assertGreater(by_path["many-lines.txt"].additions, MAX_FILE_LINES)
        self.assertIn("still visible", by_path["later.txt"].patch)
        rendered = render_preview(preview, file="file", html_budget=4_000)
        self.assertIn("per-file 500 KiB limit exceeded", rendered)

    def test_preview_preserves_renames_and_safely_renders_hostile_paths(self):
        old_oid = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        subprocess.run(["git", "-C", str(self.source), "mv", "file", "renamed file"], check=True)
        hostile_name = '<script>\u202e\nname.txt'
        (self.source / hostile_name).write_text("hostile path\n")
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "rename and hostile path"], check=True)
        new_oid = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        headers = dict(zip(self.route.metadata_headers, ["work", "example", "repo", self.ref, old_oid, new_oid]))
        submission = self.route.freeze(headers, self.bundle(), "a" * 64, 10)
        preview = load_preview(submission.preview.data)
        renamed = next(file for file in preview.files if file.path == "renamed file")
        self.assertEqual((renamed.status, renamed.old_path), ("renamed", "file"))
        self.assertIn("rename from file", renamed.patch)
        hostile = next(file for file in preview.files if file.path.startswith("<script>"))
        self.assertNotIn("\u202e", hostile.path)
        self.assertNotIn("\n", hostile.path)
        rendered = render_preview(preview, file=hostile.path, html_budget=6_000)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_git_route_represents_copied_content_as_a_created_file(self):
        old_oid = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        shutil.copyfile(self.source / "file", self.source / "copied")
        subprocess.run(["git", "-C", str(self.source), "add", "copied"], check=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "copy"], check=True)
        new_oid = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        headers = dict(zip(self.route.metadata_headers, ["work", "example", "repo", self.ref, old_oid, new_oid]))
        submission = self.route.freeze(headers, self.bundle(), "a" * 64, 10)
        copied = next(file for file in load_preview(submission.preview.data).files if file.path == "copied")
        self.assertEqual(copied.status, "created")
        self.assertIsNone(copied.old_path)
        self.assertIn("new file mode", copied.patch)

    def test_freeze_rejects_non_fast_forward_history_before_approval(self):
        old_oid = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        subprocess.run(["git", "-C", str(self.source), "checkout", "-q", "--orphan", "other"], check=True)
        subprocess.run(["git", "-C", str(self.source), "rm", "-q", "-rf", "."], check=True)
        new_oid = self.commit("other\n")
        headers = dict(zip(self.route.metadata_headers, ["work", "example", "repo", self.ref, old_oid, new_oid]))
        with self.assertRaises(ValueError):
            self.route.freeze(headers, self.bundle(), "a" * 64, 10)

    def test_create_and_fast_forward(self):
        first = self.action()
        result = self.route.execute(first, self.bundle())
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(self.remote_oid(), first.new_oid)

        second_oid = self.commit("two\n")
        second = self.action(first.new_oid, second_oid)
        result = self.route.execute(second, self.bundle())
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(self.remote_oid(), second_oid)

    def test_stale_remote_is_rejected_without_write(self):
        first = self.action()
        self.assertEqual(self.route.execute(first, self.bundle()).state, "succeeded")
        second_oid = self.commit("two\n")
        stale = self.action(ZERO_OID, second_oid)
        result = self.route.execute(stale, self.bundle())
        self.assertEqual((result.state, result.code), ("failed", "remote_drift"))
        self.assertEqual(self.remote_oid(), first.new_oid)

    def test_non_fast_forward_is_rejected(self):
        first = self.action()
        self.route.execute(first, self.bundle())
        subprocess.run(["git", "-C", str(self.source), "checkout", "-q", "--orphan", "other"], check=True)
        subprocess.run(["git", "-C", str(self.source), "rm", "-q", "-rf", "."], check=True)
        other = self.commit("other\n")
        action = self.action(first.new_oid, other)
        result = self.route.execute(action, self.bundle())
        self.assertEqual((result.state, result.code), ("failed", "not_fast_forward"))
        self.assertEqual(self.remote_oid(), first.new_oid)

    def test_multiple_bundle_heads_and_lfs_are_rejected(self):
        subprocess.run(["git", "-C", str(self.source), "branch", "other"], check=True)
        multi = self.bundle("multi.bundle", "--all")
        result = self.route.execute(self.action(), multi)
        self.assertEqual((result.state, result.code), ("failed", "bundle_heads"))

        (self.source / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
        subprocess.run(["git", "-C", str(self.source), "add", ".gitattributes"], check=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "lfs"], check=True)
        result = self.route.execute(self.action(), self.bundle("lfs.bundle"))
        self.assertEqual((result.state, result.code), ("failed", "git_lfs"))

    def test_reconciliation_outcomes(self):
        action = self.action()
        self.assertEqual(self.route.reconcile(action).code, "not_applied")
        self.route.execute(action, self.bundle())
        self.assertEqual(self.route.reconcile(action).code, "remote_confirmed")
        drift_oid = self.commit("drift\n")
        subprocess.run(
            ["git", "-C", str(self.source), "push", "--force", str(self.remote), f"{drift_oid}:{self.ref}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(self.route.reconcile(action).code, "remote_drift")
        self.route.remote = str(self.root / "missing.git")
        self.assertEqual(self.route.reconcile(action).state, "uncertain")

    def test_ssh_environment_is_scrubbed_and_hardened(self):
        route = GitPushRoute(
            self.scratch,
            Path("/run/host-ssh-agent.sock"),
            "SHA256:test",
            Path(__file__).parents[1] / "external_gate" / "github_known_hosts",
            agent_validator=lambda: None,
        )
        env = route.git_env()
        command = env["GIT_SSH_COMMAND"]
        for option in (
            "-F /dev/null", "BatchMode=yes", "StrictHostKeyChecking=yes",
            "ClearAllForwardings=yes", "ForwardAgent=no", "PermitLocalCommand=no",
            "RequestTTY=no", "PasswordAuthentication=no",
        ):
            self.assertIn(option, command)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertNotIn("USER", env)

    def test_pre_execution_agent_failure_prevents_git_work(self):
        self.route.agent_validator = lambda: (_ for _ in ()).throw(
            RouteFailure("agent_fingerprint", "dedicated SSH identity does not match configuration")
        )
        with mock.patch.object(self.route, "_prepare") as prepare:
            result = self.route.execute(self.action(), self.bundle())
        prepare.assert_not_called()
        self.assertEqual((result.state, result.code), ("failed", "agent_fingerprint"))

    def test_scratch_enospc_is_permanent_failure(self):
        with mock.patch.object(self.route, "_prepare", side_effect=OSError(errno.ENOSPC, "full")):
            result = self.route.execute(self.action(), self.bundle())
        self.assertEqual((result.state, result.code), ("failed", "scratch_full"))
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_preview_cleanup_cannot_delete_active_execution_scratch(self):
        active_push = self.scratch / "git-push-active"
        stale_preview = self.scratch / "git-preview-stale"
        active_push.mkdir()
        stale_preview.mkdir()
        self.route.cleanup_scratch(("git-preview-",))
        self.assertTrue(active_push.is_dir())
        self.assertFalse(stale_preview.exists())

    def test_cleanup_failure_forces_restart_before_reconciliation(self):
        with mock.patch("external_gate.git_push.shutil.rmtree", side_effect=OSError("cleanup failed")):
            result = self.route.execute(self.action(), self.bundle())
        self.assertEqual((result.state, result.code), ("uncertain", "scratch_cleanup"))
        self.assertTrue(result.restart_before_reconcile)

    def test_git_failure_carries_only_safe_operation_code(self):
        failed = ProcessResult(128, b"", b"agent-controlled stderr", False, True)
        with mock.patch.object(self.route, "_git_result", return_value=failed):
            with self.assertRaises(RouteFailure) as caught:
                self.route._git(self.scratch, "rev-parse", "hostile-value")
        self.assertEqual(caught.exception.code, "git_validation")
        self.assertEqual(caught.exception.operation, "rev_parse")
        self.assertNotIn("agent-controlled", str(caught.exception))

    def test_gate_logs_git_operation_without_git_output(self):
        bundle = self.bundle()
        body = bundle.read_bytes()
        offset = 0

        def read(amount):
            nonlocal offset
            chunk = body[offset : offset + amount]
            offset += len(chunk)
            return chunk

        gate = Gate(
            GateConfig(
                state_dir=self.root / "log-state",
                socket_path=self.root / "log-socket" / "request.sock",
                public_origin="http://127.0.0.1:7843",
                password="a" * 32,
            ),
            {self.route.name: self.route},
            fatal_exit=lambda: None,
        )
        new_oid = self.action().new_oid
        headers = dict(zip(
            self.route.metadata_headers,
            ["work", "example", "repo", self.ref, ZERO_OID, new_oid],
        ))
        failed = ProcessResult(128, b"", b"agent_secret_value", False, True)
        with mock.patch.object(self.route, "_git_result", return_value=failed):
            with self.assertLogs("external_gate", level="WARNING") as captured:
                with self.assertRaises(GateError):
                    gate.submit(
                        self.route.name,
                        headers,
                        len(body),
                        read,
                        gate.monotonic() + 10,
                    )
        output = "\n".join(captured.output)
        self.assertIn(
            "route=git.push-branch.v1 operation=init code=git_validation",
            output,
        )
        self.assertNotIn("agent_secret_value", output)
        self.assertNotIn(str(bundle), output)

    def test_process_runner_bounds_output_and_reaps_timeout(self):
        runner = ProcessRunner(output_limit=1024)
        noisy = runner.run(
            ["/usr/bin/python3", "-c", "import sys; sys.stdout.write('x'*100000); sys.stderr.write('y'*100000)"],
            cwd=self.root,
            env={"PATH": "/usr/bin:/bin"},
            timeout=5,
        )
        self.assertEqual(noisy.returncode, 0)
        self.assertEqual(len(noisy.stdout), 1024)
        self.assertEqual(len(noisy.stderr), 1024)
        self.assertTrue(noisy.group_stopped)
        timed = runner.run(
            ["/usr/bin/python3", "-c", "import time; time.sleep(30)"],
            cwd=self.root,
            env={"PATH": "/usr/bin:/bin"},
            timeout=0.1,
        )
        self.assertTrue(timed.timed_out)
        self.assertTrue(timed.group_stopped)

    def test_process_runner_reaps_after_internal_exception(self):
        runner = ProcessRunner()
        with mock.patch("external_gate.git_push.selectors.DefaultSelector", side_effect=OSError("selector failed")):
            result = runner.run(
                ["/usr/bin/python3", "-c", "import time; time.sleep(30)"],
                cwd=self.root,
                env={"PATH": "/usr/bin:/bin"},
                timeout=5,
            )
        self.assertTrue(result.internal_error)
        self.assertTrue(result.group_stopped)

    def test_agent_validator_rejects_identity_count_and_fingerprint(self):
        class Runner:
            def __init__(self, stdout, returncode=0):
                self.stdout = stdout
                self.returncode = returncode

            def run(self, *_args, **_kwargs):
                return ProcessResult(self.returncode, self.stdout, b"", False, True)

        socket_path = self.root / "agent.sock"
        validator = AgentValidator(socket_path, "SHA256:expected", Runner(b"256 SHA256:expected key (ED25519)\n"))
        validator()
        with self.assertRaises(Exception):
            AgentValidator(socket_path, "SHA256:expected", Runner(b"", returncode=1))()
        with self.assertRaises(Exception):
            AgentValidator(socket_path, "SHA256:expected", Runner(b""))()
        with self.assertRaises(Exception):
            AgentValidator(socket_path, "SHA256:expected", Runner(b"256 SHA256:other key (ED25519)\n"))()
        with self.assertRaises(Exception):
            AgentValidator(
                socket_path,
                "SHA256:expected",
                Runner(b"256 SHA256:expected a\n256 SHA256:other b\n"),
            )()

    def test_post_push_remote_io_error_remains_uncertain(self):
        class RemoteErrorRoute(LocalRoute):
            def __init__(self, scratch, remote):
                super().__init__(scratch, remote)
                self.remote_queries = 0

            def _prepare(self, _body_path, _action, root):
                return root

            def _remote_oid(self, _directory, action):
                self.remote_queries += 1
                if self.remote_queries == 1:
                    return action.old_oid
                raise OSError("remote query failed")

            def _git_result(self, _directory, *arguments, timeout=120):
                if "push" in arguments:
                    return ProcessResult(1, b"", b"", False, True)
                return ProcessResult(0, b"", b"", False, True)

        route = RemoteErrorRoute(self.scratch, self.remote)
        result = route.execute(self.action(), self.bundle())
        self.assertEqual(route.remote_queries, 2)
        self.assertEqual((result.state, result.code), ("uncertain", "remote_unavailable"))

    def test_unconfirmed_writer_shutdown_blocks_remote_reconciliation(self):
        class UnstoppedRoute(LocalRoute):
            def __init__(self, scratch, remote):
                super().__init__(scratch, remote)
                self.remote_queries = 0

            def _prepare(self, _body_path, _action, root):
                return root

            def _remote_oid(self, _directory, action):
                self.remote_queries += 1
                return action.old_oid

            def _git_result(self, _directory, *arguments, timeout=120):
                if "push" in arguments:
                    return ProcessResult(-9, b"", b"", True, False)
                return ProcessResult(0, b"", b"", False, True)

        route = UnstoppedRoute(self.scratch, self.remote)
        result = route.execute(self.action(), self.bundle())
        self.assertEqual(route.remote_queries, 1)
        self.assertEqual(result.state, "uncertain")
        self.assertTrue(result.restart_before_reconcile)


if __name__ == "__main__":
    unittest.main()
