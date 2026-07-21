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

from .diff_viewer import (
    MAX_EXTRA_FILE_COUNT,
    MAX_FILE_BYTES,
    MAX_FILE_LINES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    MAX_TOTAL_LINES,
    OMITTED_BINARY,
    OMITTED_FILE_BYTES,
    OMITTED_FILE_LINES,
    OMITTED_TOTAL_BYTES,
    OMITTED_TOTAL_LINES,
    FilePatch,
    OmittedFilePatch,
    build_preview,
    load_preview,
)
from .gate import (
    ActionResult,
    ActionRoute,
    ApprovalDiff,
    ApprovalSection,
    FrozenSubmission,
    PreviewPayload,
    neutralize_bidi_controls,
)

ZERO_OID = "0" * 40
MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
BUNDLE_IMPORT_TIMEOUT_SECONDS = 600
PUSH_TIMEOUT_SECONDS = 900
MAX_OUTPUT_BYTES = 64 * 1024
MAX_DIFF_PREVIEW_BYTES = 2_400
MAX_DIFF_PREVIEW_LINES = 120
MAX_DIFF_STAT_BYTES = 400
MAX_COMMIT_PREVIEW_BYTES = 600
MAX_GIT_PATH_BYTES = 4 * 1024
PREVIEW_TIMEOUT_SECONDS = 120.0
PER_FILE_TIMEOUT_SECONDS = 20.0
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
    commit_count: int = 0
    commits: str = ""
    diff_stat: str = ""
    diff: str = ""
    diff_truncated: bool = False
    sidecar_preview: bool = False


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    group_stopped: bool
    internal_error: bool = False


@dataclass(frozen=True)
class ChangedPath:
    path: str
    old_path: str | None
    status: str


class NameStatusCollector:
    """Parse Git's NUL-delimited changed-file stream with bounded retention."""

    def __init__(self):
        self.buffer = bytearray()
        self.status: str | None = None
        self.paths: list[bytes] = []
        self.changed: list[ChangedPath] = []
        self.total = 0

    @staticmethod
    def _status(token: bytes) -> str:
        try:
            status_token = token.decode("ascii")
        except UnicodeDecodeError as error:
            raise ValueError("invalid Git file status") from error
        match = re.fullmatch(r"([ACDMRTUXB])([0-9]{1,3})?", status_token)
        if match is None or (match.group(2) is not None and int(match.group(2)) > 100):
            raise ValueError("invalid Git file status")
        return match.group(1)

    def _token(self, token: bytes) -> None:
        if self.status is None:
            self.status = self._status(token)
            return
        if not token or len(token) > MAX_GIT_PATH_BYTES:
            raise ValueError("invalid Git path")
        self.paths.append(token)
        required = 2 if self.status in {"R", "C"} else 1
        if len(self.paths) < required:
            return
        if len(self.paths) != required:
            raise ValueError("invalid Git changed-file list")
        self.total += 1
        if self.total > MAX_FILES + MAX_EXTRA_FILE_COUNT:
            raise ValueError("Git changed-file count is too large")
        if len(self.changed) < MAX_FILES:
            if self.status in {"R", "C"}:
                old_path = os.fsdecode(self.paths[0])
                path = os.fsdecode(self.paths[1])
            else:
                old_path = None
                path = os.fsdecode(self.paths[0])
            status = {
                "A": "created",
                "D": "deleted",
                "R": "renamed",
                "C": "copied",
            }.get(self.status, "modified")
            self.changed.append(ChangedPath(path, old_path, status))
        self.status = None
        self.paths = []

    def __call__(self, chunk: bytes) -> None:
        self.buffer.extend(chunk)
        while True:
            separator = self.buffer.find(0)
            if separator < 0:
                limit = 4 if self.status is None else MAX_GIT_PATH_BYTES
                if len(self.buffer) > limit:
                    raise ValueError("Git preview metadata token is too large")
                return
            token = bytes(self.buffer[:separator])
            del self.buffer[: separator + 1]
            self._token(token)

    def finish(self) -> tuple[list[ChangedPath], int]:
        if self.buffer or self.status is not None or self.paths:
            raise ValueError("invalid Git changed-file list")
        return self.changed, self.total - len(self.changed)


class PatchCapture:
    """Retain at most one per-file allowance while counting the full stream."""

    def __init__(self):
        self.data = bytearray()
        self.byte_count = 0
        self.newlines = 0
        self.last_byte: int | None = None
        self.stopped_retaining = False
        self.line_prefix = bytearray()
        self.in_hunk = False
        self.additions = 0
        self.deletions = 0
        self.finished = False

    def _finish_line(self) -> None:
        prefix = bytes(self.line_prefix)
        if prefix.startswith(b"@@"):
            self.in_hunk = True
        elif self.in_hunk and prefix.startswith(b"+"):
            self.additions += 1
        elif self.in_hunk and prefix.startswith(b"-"):
            self.deletions += 1
        self.line_prefix.clear()

    def __call__(self, chunk: bytes) -> None:
        if self.finished:
            raise ValueError("Git patch capture is already finished")
        pieces = chunk.split(b"\n")
        for index, piece in enumerate(pieces):
            room = 2 - len(self.line_prefix)
            if room > 0:
                self.line_prefix.extend(piece[:room])
            if index + 1 < len(pieces):
                self._finish_line()
        self.byte_count += len(chunk)
        self.newlines += len(pieces) - 1
        if chunk:
            self.last_byte = chunk[-1]
        if self.stopped_retaining:
            return
        byte_room = MAX_FILE_BYTES + 1 - len(self.data)
        candidate = chunk[:max(0, byte_room)]
        line_room = MAX_FILE_LINES + 1 - self.data.count(b"\n")
        if line_room <= 0:
            candidate = b""
        elif candidate.count(b"\n") >= line_room:
            end = -1
            cursor = 0
            for _ in range(line_room):
                end = candidate.find(b"\n", cursor)
                cursor = end + 1
            candidate = candidate[: end + 1]
        self.data.extend(candidate)
        if len(self.data) > MAX_FILE_BYTES or self.data.count(b"\n") > MAX_FILE_LINES:
            self.stopped_retaining = True

    def finish(self) -> None:
        if self.finished:
            return
        if self.byte_count and self.last_byte != ord("\n"):
            self._finish_line()
        self.finished = True

    @property
    def line_count(self) -> int:
        return self.newlines + (1 if self.byte_count and self.last_byte != ord("\n") else 0)


def parse_name_status(data: bytes) -> list[ChangedPath]:
    collector = NameStatusCollector()
    collector(data)
    changed, _extra = collector.finish()
    return changed


class RouteFailure(Exception):
    def __init__(self, code: str, message: str, operation: str = "route"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.operation = operation


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


def bounded_preview(data: bytes, limit: int, max_lines: int | None = None) -> tuple[str, bool]:
    text = neutralize_bidi_controls(data.decode("utf-8", "replace"))
    safe = "".join(char if ord(char) >= 32 or char in {"\n", "\t"} else "�" for char in text)
    truncated = False
    if max_lines is not None:
        lines = safe.splitlines()
        if len(lines) > max_lines:
            safe = "\n".join(lines[:max_lines])
            truncated = True
    encoded = safe.encode("utf-8")
    truncated = truncated or len(encoded) > limit
    if truncated:
        encoded = encoded[:limit]
        while True:
            try:
                safe = encoded.decode("utf-8")
                break
            except UnicodeDecodeError:
                encoded = encoded[:-1]
    return safe.rstrip(), truncated


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

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        stdout_consumer: Callable[[bytes], None] | None = None,
    ) -> ProcessResult:
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
                    if key.fd == stdout_fd and stdout_consumer is not None:
                        stdout_consumer(chunk)
                    else:
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
                        if descriptor == stdout_fd and stdout_consumer is not None:
                            stdout_consumer(chunk)
                        else:
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
    display_name = "Push branch to GitHub"
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
    diagnostic_operations = frozenset({
        "bundle",
        "diff",
        "fetch",
        "fsck",
        "git",
        "hash_object",
        "init",
        "log",
        "rev_list",
        "rev_parse",
    })
    diagnostic_codes = frozenset({
        "attributes_check",
        "bundle_commit",
        "bundle_heads",
        "git_internal",
        "git_lfs",
        "git_preview",
        "git_preview_timeout",
        "git_process_unstopped",
        "git_timeout",
        "git_validation",
        "not_fast_forward",
        "scratch_cleanup",
        "scratch_full",
    })

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

    def freeze(self, metadata, body_path, _digest, _size):
        identity = [
            metadata["X-Vivarium-Profile"],
            metadata["X-Vivarium-Owner"],
            metadata["X-Vivarium-Repo"],
            metadata["X-Vivarium-Ref"],
            metadata["X-Vivarium-Old-Oid"],
            metadata["X-Vivarium-New-Oid"],
        ]
        validate_fields(*identity)
        action = GitPushAction(*identity)
        try:
            commit_count, commits, diff_stat, preview = self._build_preview(body_path, action)
        except (IsolationLost, RouteFailure, ScratchCleanupError, OSError) as error:
            raise ValueError("Git push preview validation failed") from error
        frozen = {
            "profile": action.profile,
            "owner": action.owner,
            "repo": action.repo,
            "ref": action.ref,
            "old_oid": action.old_oid,
            "new_oid": action.new_oid,
            "commit_count": commit_count,
            "commits": commits,
            "diff_stat": diff_stat,
        }
        self.decode(frozen)
        return FrozenSubmission(frozen, PreviewPayload("diff.v1", preview))

    def decode(self, frozen) -> GitPushAction:
        identity_keys = {"profile", "owner", "repo", "ref", "old_oid", "new_oid"}
        inline_preview_keys = {"commit_count", "commits", "diff_stat", "diff", "diff_truncated"}
        sidecar_preview_keys = {"commit_count", "commits", "diff_stat"}
        if not isinstance(frozen, dict):
            raise ValueError("invalid Git push action")
        keys = frozenset(frozen)
        if keys not in {
            frozenset(identity_keys),
            frozenset(identity_keys | inline_preview_keys),
            frozenset(identity_keys | sidecar_preview_keys),
        }:
            raise ValueError("invalid Git push action")
        identity = [frozen[key] for key in ("profile", "owner", "repo", "ref", "old_oid", "new_oid")]
        validate_fields(*identity)
        checked = self._git_result(
            self.scratch_dir, "check-ref-format", "--branch", identity[3].removeprefix("refs/heads/"), timeout=5
        )
        if checked.internal_error or checked.timed_out or not checked.group_stopped or checked.returncode != 0:
            raise ValueError("invalid Git branch")
        if keys == identity_keys:
            return GitPushAction(*identity)
        if (
            not isinstance(frozen["commit_count"], int)
            or isinstance(frozen["commit_count"], bool)
            or frozen["commit_count"] <= 0
            or ("diff_truncated" in frozen and not isinstance(frozen["diff_truncated"], bool))
        ):
            raise ValueError("invalid Git push preview")
        bounded_fields = [
            ("commits", MAX_COMMIT_PREVIEW_BYTES),
            ("diff_stat", MAX_DIFF_STAT_BYTES),
        ]
        if "diff" in frozen:
            bounded_fields.append(("diff", MAX_DIFF_PREVIEW_BYTES))
        for key, limit in bounded_fields:
            value = frozen[key]
            if (
                not isinstance(value, str)
                or len(value.encode("utf-8")) > limit
                or any(ord(char) < 32 and char not in {"\n", "\t"} for char in value)
            ):
                raise ValueError("invalid Git push preview")
        if keys == identity_keys | sidecar_preview_keys:
            return GitPushAction(
                *identity,
                frozen["commit_count"],
                frozen["commits"],
                frozen["diff_stat"],
                sidecar_preview=True,
            )
        return GitPushAction(
            *identity,
            frozen["commit_count"],
            frozen["commits"],
            frozen["diff_stat"],
            frozen["diff"],
            frozen["diff_truncated"],
        )

    def describe(self, action: GitPushAction):
        fields = [
            ("Repository", f"{action.owner}/{action.repo}"),
            ("Branch", action.ref.removeprefix("refs/heads/")),
        ]
        if action.commit_count:
            noun = "commit" if action.commit_count == 1 else "commits"
            change = f"{action.commit_count} {noun}"
        else:
            change = "Preview unavailable"
        fields.extend((("Change", change), ("Profile", action.profile)))
        return fields + [
            ("Expected old commit", action.old_oid),
            ("Approved new commit", action.new_oid),
        ]

    def approval_sections(self, action: GitPushAction):
        if not action.commit_count:
            return []
        noun = "commit" if action.commit_count == 1 else "commits"
        sections = []
        if not action.sidecar_preview:
            sections.append(
                ApprovalSection(
                    "Changes in this push",
                    "diff",
                    action.diff,
                    action.diff_stat or "No file content changes",
                    action.diff_truncated,
                )
            )
        if action.commits:
            sections.append(
                ApprovalSection("Commits", "code", action.commits, f"{action.commit_count} {noun}")
            )
        return sections

    def approval_diff(self, action: GitPushAction) -> ApprovalDiff | None:
        if not action.sidecar_preview:
            return None
        return ApprovalDiff("Changes in this push", action.diff_stat or "No file content changes")

    def _build_preview(self, body_path: Path, action: GitPushAction) -> tuple[int, str, str, bytes]:
        with self._scratch("git-preview-") as root:
            repository = self._prepare(body_path, action, root)
            if action.old_oid == ZERO_OID:
                base = self._git(repository, "hash-object", "-t", "tree", "-w", "--stdin", timeout=5).strip()
                revision = action.new_oid
            else:
                present = self._git_result(repository, "cat-file", "-e", f"{action.old_oid}^{{commit}}", timeout=5)
                if not present.group_stopped:
                    raise IsolationLost("git_process_unstopped", "Git preview process did not stop cleanly")
                if present.internal_error or present.timed_out:
                    raise RouteFailure("git_preview", "Git preview validation failed")
                if present.returncode != 0:
                    raise RouteFailure("not_fast_forward", "expected remote base is not in submitted history")
                base = action.old_oid
                revision = f"{action.old_oid}..{action.new_oid}"

            count_text = self._git(repository, "rev-list", "--count", revision, timeout=10).strip()
            if not re.fullmatch(r"[0-9]+", count_text):
                raise RouteFailure("git_preview", "Git commit preview is invalid")
            commit_count = int(count_text)
            commits, _ = bounded_preview(
                self._git(
                    repository, "log", "--max-count=8", "--format=%h%x09%s", revision, timeout=10
                ).encode(),
                MAX_COMMIT_PREVIEW_BYTES,
            )
            stat_text, _ = bounded_preview(
                self._git(
                    repository, "diff", "--stat=90,24", "--compact-summary", "--no-ext-diff",
                    "--no-textconv", "--find-renames", base, action.new_oid, "--", timeout=20,
                ).encode(),
                MAX_DIFF_STAT_BYTES,
            )
            preview = self._build_diff_artifact(repository, base, action.new_oid)
            return commit_count, commits, stat_text, preview

    @staticmethod
    def _require_preview_process(result: ProcessResult) -> None:
        if not result.group_stopped:
            raise IsolationLost("git_process_unstopped", "Git preview process did not stop cleanly")
        if result.internal_error or result.timed_out or result.returncode != 0:
            raise RouteFailure("git_preview", "Git diff preview generation failed")

    def _build_diff_artifact(self, repository: Path, base: str, new_oid: str) -> bytes:
        deadline = time.monotonic() + PREVIEW_TIMEOUT_SECONDS
        names = NameStatusCollector()
        result = self._git_result(
            repository,
            "diff",
            "--name-status",
            "-z",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            base,
            new_oid,
            "--",
            timeout=min(PER_FILE_TIMEOUT_SECONDS, max(0.1, deadline - time.monotonic())),
            stdout_consumer=names,
        )
        self._require_preview_process(result)
        changed_paths, extra_file_count = names.finish()
        files: list[FilePatch | OmittedFilePatch] = []
        included_bytes = 0
        included_lines = 0
        for changed in changed_paths[:MAX_FILES]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RouteFailure("git_preview_timeout", "Git diff preview generation timed out")
            capture = PatchCapture()
            pathspecs = [f":(top,literal){changed.path}"]
            if changed.status == "renamed" and changed.old_path is not None:
                pathspecs.insert(0, f":(top,literal){changed.old_path}")
            result = self._git_result(
                repository,
                "diff",
                "--full-index",
                "--no-ext-diff",
                "--no-textconv",
                "--find-renames",
                "--unified=3",
                base,
                new_oid,
                "--",
                *pathspecs,
                timeout=min(PER_FILE_TIMEOUT_SECONDS, max(0.1, remaining)),
                stdout_consumer=capture,
            )
            self._require_preview_process(result)
            capture.finish()
            if capture.byte_count > MAX_FILE_BYTES:
                files.append(
                    OmittedFilePatch(
                        changed.path,
                        OMITTED_FILE_BYTES,
                        capture.byte_count,
                        capture.line_count,
                        capture.additions,
                        capture.deletions,
                        old_path=changed.old_path,
                        status=changed.status,
                    )
                )
                continue
            if capture.line_count > MAX_FILE_LINES:
                files.append(
                    OmittedFilePatch(
                        changed.path,
                        OMITTED_FILE_LINES,
                        capture.byte_count,
                        capture.line_count,
                        capture.additions,
                        capture.deletions,
                        old_path=changed.old_path,
                        status=changed.status,
                    )
                )
                continue
            source = FilePatch(
                changed.path,
                bytes(capture.data),
                old_path=changed.old_path,
                status=changed.status,
            )
            summary = load_preview(build_preview([source])).files[0]
            reason = summary.omission_reason
            if reason is None and included_bytes + summary.byte_count > MAX_TOTAL_BYTES:
                reason = OMITTED_TOTAL_BYTES
            elif reason is None and included_lines + summary.line_count > MAX_TOTAL_LINES:
                reason = OMITTED_TOTAL_LINES
            if reason is None:
                files.append(source)
                included_bytes += summary.byte_count
                included_lines += summary.line_count
            else:
                files.append(
                    OmittedFilePatch(
                        changed.path,
                        reason,
                        summary.byte_count,
                        summary.line_count,
                        summary.additions,
                        summary.deletions,
                        changed.old_path,
                        summary.status,
                        summary.binary,
                    )
                )

        return build_preview(files, extra_file_count=extra_file_count)

    def remote_url(self, action: GitPushAction) -> str:
        return f"git@github.com:{action.owner}/{action.repo}.git"

    def cleanup_scratch(
        self, prefixes: tuple[str, ...] = ("git-push-", "git-reconcile-", "git-preview-")
    ) -> None:
        for entry in self.scratch_dir.iterdir():
            if entry.name.startswith(prefixes):
                try:
                    shutil.rmtree(entry)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise ScratchCleanupError("scratch_cleanup", "Git scratch cleanup failed") from error

    @contextlib.contextmanager
    def _scratch(self, prefix: str):
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

    def _git_result(
        self,
        directory: Path,
        *arguments: str,
        timeout: float = 120,
        stdout_consumer: Callable[[bytes], None] | None = None,
    ) -> ProcessResult:
        command = [
            "/usr/bin/git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "credential.helper=",
            "-c", "protocol.allow=never",
            "-c", "protocol.file.allow=always",
            "-c", "protocol.ssh.allow=always",
            *arguments,
        ]
        return self.runner.run(
            command,
            cwd=directory,
            env=self.git_env(),
            timeout=timeout,
            stdout_consumer=stdout_consumer,
        )

    def _git(self, directory: Path, *arguments: str, timeout: float = 120) -> str:
        result = self._git_result(directory, *arguments, timeout=timeout)
        operation = arguments[0].replace("-", "_") if arguments else "git"
        if re.fullmatch(r"[a-z][a-z0-9_]{0,31}", operation) is None:
            operation = "git"
        if not result.group_stopped:
            raise IsolationLost(
                "git_process_unstopped",
                "Git process did not stop cleanly",
                operation,
            )
        if result.internal_error:
            raise RouteFailure("git_internal", "Git validation failed internally", operation)
        if result.timed_out:
            raise RouteFailure("git_timeout", "Git validation timed out", operation)
        if result.returncode != 0:
            if b"No space left on device" in result.stderr:
                raise RouteFailure(
                    "scratch_full",
                    "bounded Git scratch space is full",
                    operation,
                )
            raise RouteFailure("git_validation", "Git validation failed", operation)
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
        self._git(
            repository, "fetch", "--quiet", str(body_path), "HEAD:refs/heads/candidate",
            timeout=BUNDLE_IMPORT_TIMEOUT_SECONDS,
        )
        if self._git(repository, "rev-parse", "refs/heads/candidate").strip() != action.new_oid:
            raise RouteFailure("bundle_commit", "bundle commit does not match approval")
        self._git(
            repository, "fsck", "--strict", "--no-reflogs", action.new_oid,
            timeout=BUNDLE_IMPORT_TIMEOUT_SECONDS,
        )
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
                    timeout=PUSH_TIMEOUT_SECONDS,
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
