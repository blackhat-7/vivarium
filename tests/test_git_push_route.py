import errno
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from external_gate.gate import ActionResult
from external_gate.git_push import (
    AgentValidator,
    GitPushAction,
    GitPushRoute,
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
        frozen = self.route.freeze(headers, self.bundle(), "a" * 64, 10)
        self.assertEqual(
            set(frozen),
            {
                "profile", "owner", "repo", "ref", "old_oid", "new_oid", "commit_count",
                "commits", "diff_stat", "diff", "diff_truncated",
            },
        )
        action = self.route.decode(frozen)
        self.assertEqual(action.new_oid, new_oid)
        self.assertEqual(action.commit_count, 1)
        self.assertIn("+one", action.diff)
        self.assertEqual(self.route.describe(action)[1], ("Repository", "example/repo"))
        sections = self.route.approval_sections(action)
        self.assertEqual([section.kind for section in sections], ["code", "diff"])

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
        self.assertNotIn("Change", [label for label, _value in self.route.describe(action)])

    def test_diff_preview_is_bounded_and_marks_truncation(self):
        old_oid = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        new_oid = self.commit("x" * 6_000 + "\n")
        headers = dict(zip(self.route.metadata_headers, ["work", "example", "repo", self.ref, old_oid, new_oid]))
        frozen = self.route.freeze(headers, self.bundle(), "a" * 64, 10)
        self.assertTrue(frozen["diff_truncated"])
        self.assertLessEqual(len(frozen["diff"].encode()), 2_400)
        self.assertLessEqual(len(frozen["diff"].splitlines()), 120)

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
