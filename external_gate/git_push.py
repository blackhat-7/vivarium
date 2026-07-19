"""Reviewed GitHub branch-push route for the external gate."""

from __future__ import annotations

import contextlib
import errno
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .gate import ActionResult, ActionRoute

ZERO_OID = "0" * 40
MAX_BUNDLE_BYTES = 100 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
OID_RE = re.compile(r"^[0-9a-f]{40}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


@dataclass(frozen=True)
class GitPushAction:
    profile: str
    owner: str
    repo: str
    ref: str
    old_oid: str
    new_oid: str


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    group_stopped: bool
    internal_error: bool = False


class RouteFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RemoteUnavailable(RouteFailure):
    pass


class IsolationLost(RouteFailure):
    pass


class ScratchCleanupError(RouteFailure):
    pass


def valid_ref(ref: str) -> bool:
    if not isinstance(ref, str) or not ref.isascii() or not ref.startswith("refs/heads/") or len(ref) > 250:
        return False
    name = ref.removeprefix("refs/heads/")
    if not name or name == "@" or name.startswith((".", "-")) or name.endswith(("/", ".")):
        return False
    if any(component.startswith(".") or component.endswith(".lock") for component in name.split("/")):
        return False
    forbidden = ("..", "//", "@{", " ", "~", "^", ":", "?", "*", "[", "\\")
    return not any(item in name for item in forbidden) and all(32 <= ord(char) < 127 for char in name)


def validate_fields(profile: str, owner: str, repo: str, ref: str, old_oid: str, new_oid: str) -> None:
    if not isinstance(profile, str) or PROFILE_RE.fullmatch(profile) is None:
        raise ValueError("invalid profile")
    if not isinstance(owner, str) or OWNER_RE.fullmatch(owner) is None:
        raise ValueError("invalid owner")
    if (
        not isinstance(repo, str)
        or REPO_RE.fullmatch(repo) is None
        or repo in {".", ".."}
        or repo.endswith(".git")
    ):
        raise ValueError("invalid repository")
    if not valid_ref(ref):
        raise ValueError("invalid branch")
    if not isinstance(old_oid, str) or OID_RE.fullmatch(old_oid) is None:
        raise ValueError("invalid old commit")
    if not isinstance(new_oid, str) or OID_RE.fullmatch(new_oid) is None or new_oid in {ZERO_OID, old_oid}:
        raise ValueError("invalid new commit")


class ProcessRunner:
    """Runs a bounded process in its own session and proves group shutdown."""

    def __init__(self, *, output_limit: int = MAX_OUTPUT_BYTES, monotonic: Callable[[], float] = time.monotonic):
        self.output_limit = output_limit
        self.monotonic = monotonic

    @staticmethod
    def _group_exists(group_id: int) -> bool:
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _wait_group_gone(self, group_id: int, deadline: float) -> bool:
        while self.monotonic() < deadline:
            if not self._group_exists(group_id):
                return True
            time.sleep(0.02)
        return not self._group_exists(group_id)

    def _stop_group(self, process: subprocess.Popen) -> bool:
        group_id = process.pid
        if self._group_exists(group_id):
            try:
                os.killpg(group_id, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if not self._wait_group_gone(group_id, self.monotonic() + 1.0):
                try:
                    os.killpg(group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self._wait_group_gone(group_id, self.monotonic() + 1.0)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.kill(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                return False
        return not self._group_exists(group_id)

    def run(self, command: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> ProcessResult:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
        assert process.stdout is not None and process.stderr is not None
        stdout_fd = process.stdout.fileno()
        stderr_fd = process.stderr.fileno()
        output = {stdout_fd: bytearray(), stderr_fd: bytearray()}
        selector = None
        timed_out = False
        try:
            selector = selectors.DefaultSelector()
            for descriptor in output:
                os.set_blocking(descriptor, False)
                selector.register(descriptor, selectors.EVENT_READ)
            deadline = self.monotonic() + timeout
            while selector.get_map():
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                events = selector.select(min(0.1, remaining))
                for key, _mask in events:
                    try:
                        chunk = os.read(key.fd, 16 * 1024)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    room = self.output_limit - len(output[key.fd])
                    if room > 0:
                        output[key.fd].extend(chunk[:room])
                if process.poll() is not None and not events:
                    for descriptor in list(selector.get_map()):
                        try:
                            chunk = os.read(descriptor, 16 * 1024)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(descriptor)
                            continue
                        room = self.output_limit - len(output[descriptor])
                        if room > 0:
                            output[descriptor].extend(chunk[:room])
            if timed_out:
                group_stopped = self._stop_group(process)
            else:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    group_stopped = self._stop_group(process)
                else:
                    group_stopped = not self._group_exists(process.pid) or self._stop_group(process)
            return ProcessResult(
                process.returncode if process.returncode is not None else -signal.SIGKILL,
                bytes(output[stdout_fd]),
                bytes(output[stderr_fd]),
                timed_out,
                group_stopped,
            )
        except Exception:
            try:
                group_stopped = self._stop_group(process)
            except Exception:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=1)
                group_stopped = not self._group_exists(process.pid)
            return ProcessResult(
                process.returncode if process.returncode is not None else -signal.SIGKILL,
                bytes(output[stdout_fd]),
                bytes(output[stderr_fd]),
                timed_out,
                group_stopped,
                internal_error=True,
            )
        finally:
            if selector is not None:
                with contextlib.suppress(Exception):
                    selector.close()
            with contextlib.suppress(Exception):
                process.stdout.close()
            with contextlib.suppress(Exception):
                process.stderr.close()


class AgentValidator:
    def __init__(self, ssh_socket: Path, expected_fingerprint: str, runner: ProcessRunner):
        self.ssh_socket = ssh_socket
        self.expected_fingerprint = expected_fingerprint
        self.runner = runner

    def __call__(self) -> None:
        result = self.runner.run(
            ["/usr/bin/ssh-add", "-l"],
            cwd=self.ssh_socket.parent,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "SSH_AUTH_SOCK": str(self.ssh_socket),
            },
            timeout=5,
        )
        if not result.group_stopped:
            raise IsolationLost("agent_check_unstopped", "credential check did not stop cleanly")
        if result.internal_error or result.timed_out or result.returncode != 0:
            raise RouteFailure("agent_unavailable", "dedicated SSH agent is unavailable")
        lines = result.stdout.decode("utf-8", "replace").splitlines()
        if len(lines) != 1:
            raise RouteFailure("agent_identity_count", "dedicated SSH agent must contain exactly one identity")
        fields = lines[0].split()
        if len(fields) < 2 or fields[1] != self.expected_fingerprint:
            raise RouteFailure("agent_fingerprint", "dedicated SSH identity does not match configuration")


class GitPushRoute(ActionRoute):
    name = "git.push-branch.v1"
    content_type = "application/x-git-bundle"
    max_body_bytes = MAX_BUNDLE_BYTES
    metadata_headers = (
        "X-Vivarium-Profile",
        "X-Vivarium-Owner",
        "X-Vivarium-Repo",
        "X-Vivarium-Ref",
        "X-Vivarium-Old-Oid",
        "X-Vivarium-New-Oid",
    )

    def __init__(
        self,
        scratch_dir: Path,
        ssh_socket: Path,
        ssh_fingerprint: str,
        known_hosts: Path,
        *,
        runner: ProcessRunner | None = None,
        agent_validator: Callable[[], None] | None = None,
    ):
        self.scratch_dir = scratch_dir
        self.ssh_socket = ssh_socket
        self.ssh_fingerprint = ssh_fingerprint
        self.known_hosts = known_hosts
        self.runner = runner or ProcessRunner()
        self.agent_validator = agent_validator or AgentValidator(ssh_socket, ssh_fingerprint, self.runner)

    def freeze(self, metadata, _body_path, _digest, _size):
        action = {
            "profile": metadata["X-Vivarium-Profile"],
            "owner": metadata["X-Vivarium-Owner"],
            "repo": metadata["X-Vivarium-Repo"],
            "ref": metadata["X-Vivarium-Ref"],
            "old_oid": metadata["X-Vivarium-Old-Oid"],
            "new_oid": metadata["X-Vivarium-New-Oid"],
        }
        self.decode(action)
        return action

    def decode(self, frozen) -> GitPushAction:
        keys = {"profile", "owner", "repo", "ref", "old_oid", "new_oid"}
        if not isinstance(frozen, dict) or set(frozen) != keys:
            raise ValueError("invalid Git push action")
        values = [frozen[key] for key in ("profile", "owner", "repo", "ref", "old_oid", "new_oid")]
        validate_fields(*values)
        checked = self._git_result(
            self.scratch_dir, "check-ref-format", "--branch", values[3].removeprefix("refs/heads/"), timeout=5
        )
        if checked.internal_error or checked.timed_out or not checked.group_stopped or checked.returncode != 0:
            raise ValueError("invalid Git branch")
        return GitPushAction(*values)

    def describe(self, action: GitPushAction):
        return [
            ("Profile", action.profile),
            ("Repository", f"{action.owner}/{action.repo}"),
            ("Branch", action.ref),
            ("Expected old commit", action.old_oid),
            ("Approved new commit", action.new_oid),
        ]

    def remote_url(self, action: GitPushAction) -> str:
        return f"git@github.com:{action.owner}/{action.repo}.git"

    def cleanup_scratch(self) -> None:
        for entry in self.scratch_dir.iterdir():
            if entry.name.startswith(("git-push-", "git-reconcile-")):
                try:
                    shutil.rmtree(entry)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise ScratchCleanupError("scratch_cleanup", "Git scratch cleanup failed") from error

    @contextlib.contextmanager
    def _scratch(self, prefix: str):
        self.cleanup_scratch()
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=self.scratch_dir))
        try:
            yield path
        finally:
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise ScratchCleanupError("scratch_cleanup", "Git scratch cleanup failed") from error

    def git_env(self) -> dict[str, str]:
        ssh_command = " ".join([
            "/usr/bin/ssh", "-F", "/dev/null",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "ClearAllForwardings=yes",
            "-o", "ForwardAgent=no",
            "-o", "PermitLocalCommand=no",
            "-o", "RequestTTY=no",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
        ])
        return {
            "HOME": "/nonexistent",
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
            "SSH_AUTH_SOCK": str(self.ssh_socket),
            "GIT_SSH_COMMAND": ssh_command,
        }

    def _git_result(self, directory: Path, *arguments: str, timeout: float = 120) -> ProcessResult:
        command = [
            "/usr/bin/git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "credential.helper=",
            "-c", "protocol.allow=never",
            "-c", "protocol.file.allow=always",
            "-c", "protocol.ssh.allow=always",
            *arguments,
        ]
        return self.runner.run(command, cwd=directory, env=self.git_env(), timeout=timeout)

    def _git(self, directory: Path, *arguments: str, timeout: float = 120) -> str:
        result = self._git_result(directory, *arguments, timeout=timeout)
        if not result.group_stopped:
            raise IsolationLost("git_process_unstopped", "Git process did not stop cleanly")
        if result.internal_error:
            raise RouteFailure("git_internal", "Git validation failed internally")
        if result.timed_out:
            raise RouteFailure("git_timeout", "Git validation timed out")
        if result.returncode != 0:
            if b"No space left on device" in result.stderr:
                raise RouteFailure("scratch_full", "bounded Git scratch space is full")
            raise RouteFailure("git_validation", "Git validation failed")
        return result.stdout.decode("utf-8", "replace")

    def _remote_oid(self, directory: Path, action: GitPushAction) -> str:
        result = self._git_result(directory, "ls-remote", "--refs", self.remote_url(action), action.ref, timeout=30)
        if not result.group_stopped:
            raise IsolationLost("remote_process_unstopped", "remote query did not stop cleanly")
        if result.internal_error or result.timed_out or result.returncode != 0:
            raise RemoteUnavailable("remote_unavailable", "remote state is unavailable")
        output = result.stdout.decode("utf-8", "replace").strip()
        if not output:
            return ZERO_OID
        lines = output.splitlines()
        if len(lines) != 1:
            raise RemoteUnavailable("remote_invalid", "remote returned an invalid branch advertisement")
        fields = lines[0].split()
        if len(fields) != 2 or fields[1] != action.ref or OID_RE.fullmatch(fields[0]) is None:
            raise RemoteUnavailable("remote_invalid", "remote returned an invalid branch advertisement")
        return fields[0]

    def _prepare(self, body_path: Path, action: GitPushAction, root: Path) -> Path:
        repository = root / "repo.git"
        self._git(root, "init", "--bare", "--quiet", str(repository))
        heads = self._git(repository, "bundle", "list-heads", str(body_path)).splitlines()
        if heads != [f"{action.new_oid} HEAD"]:
            raise RouteFailure("bundle_heads", "bundle does not contain exactly the approved HEAD")
        self._git(repository, "fetch", "--quiet", str(body_path), "HEAD:refs/heads/candidate")
        if self._git(repository, "rev-parse", "refs/heads/candidate").strip() != action.new_oid:
            raise RouteFailure("bundle_commit", "bundle commit does not match approval")
        self._git(repository, "fsck", "--strict", "--no-reflogs", action.new_oid)
        lfs = self._git_result(
            repository,
            "grep", "-I", "-q", "-e", "filter=lfs", action.new_oid, "--",
            ".gitattributes", ":(glob)**/.gitattributes",
        )
        if not lfs.group_stopped:
            raise IsolationLost("git_process_unstopped", "Git process did not stop cleanly")
        if lfs.internal_error or lfs.timed_out or lfs.returncode not in {0, 1}:
            raise RouteFailure("attributes_check", "could not inspect Git attributes")
        if lfs.returncode == 0:
            raise RouteFailure("git_lfs", "Git LFS pushes are not supported")
        return repository

    @staticmethod
    def _result_for_remote(action: GitPushAction, remote_oid: str) -> ActionResult:
        if remote_oid == action.new_oid:
            return ActionResult("succeeded", "remote_confirmed", "remote has the approved commit")
        if remote_oid == action.old_oid:
            return ActionResult("failed", "not_applied", "remote remains at the expected old commit; submit again")
        return ActionResult("failed", "remote_drift", "remote branch changed to another commit")

    def execute(self, action: GitPushAction, body_path: Path) -> ActionResult:
        try:
            self.agent_validator()
            with self._scratch("git-push-") as root:
                repository = self._prepare(body_path, action, root)
                try:
                    current = self._remote_oid(repository, action)
                except RemoteUnavailable:
                    return ActionResult("failed", "remote_unavailable_before_write", "remote state was unavailable before the write")
                if current == action.new_oid:
                    return ActionResult("succeeded", "already_applied", "remote already has the approved commit")
                if current != action.old_oid:
                    return ActionResult("failed", "remote_drift", "remote branch changed after submission")
                if action.old_oid != ZERO_OID:
                    ancestor = self._git_result(repository, "merge-base", "--is-ancestor", action.old_oid, action.new_oid)
                    if not ancestor.group_stopped:
                        raise IsolationLost("git_process_unstopped", "Git process did not stop cleanly")
                    if ancestor.internal_error or ancestor.timed_out or ancestor.returncode != 0:
                        return ActionResult("failed", "not_fast_forward", "approved update is not a fast-forward")
                lease = f"--force-with-lease={action.ref}:" + ("" if action.old_oid == ZERO_OID else action.old_oid)
                pushed = self._git_result(
                    repository,
                    "push", "--porcelain", "--no-verify", lease,
                    self.remote_url(action), f"{action.new_oid}:{action.ref}",
                    timeout=180,
                )
                if not pushed.group_stopped:
                    return ActionResult(
                        "uncertain", "writer_unstopped", "writer shutdown could not be proven",
                        restart_before_reconcile=True,
                    )
                if pushed.returncode == 0 and not pushed.timed_out and not pushed.internal_error:
                    return ActionResult("succeeded", "pushed", "exact approved commit pushed")
                try:
                    return self._result_for_remote(action, self._remote_oid(repository, action))
                except (RemoteUnavailable, OSError):
                    return ActionResult("uncertain", "remote_unavailable", "push outcome could not be confirmed")
        except (IsolationLost, ScratchCleanupError) as error:
            return ActionResult(
                "uncertain", error.code, error.message,
                restart_before_reconcile=True,
            )
        except RouteFailure as error:
            return ActionResult("failed", error.code, error.message)
        except OSError as error:
            if error.errno == errno.ENOSPC:
                return ActionResult("failed", "scratch_full", "bounded Git scratch space is full")
            return ActionResult("failed", "local_io", "local Git verification failed")

    def reconcile(self, action: GitPushAction) -> ActionResult:
        try:
            self.agent_validator()
            with self._scratch("git-reconcile-") as temporary:
                remote_oid = self._remote_oid(temporary, action)
            return self._result_for_remote(action, remote_oid)
        except (RemoteUnavailable, RouteFailure, OSError):
            return ActionResult("uncertain", "remote_unavailable", "remote state remains unavailable")
