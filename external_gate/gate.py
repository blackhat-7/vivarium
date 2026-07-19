"""Bounded, crash-durable core for typed host-approved actions."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import shutil
import socket
import socketserver
import stat
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

SCHEMA_VERSION = 1
MAX_OPEN_REQUESTS = 20
MAX_OPEN_BODY_BYTES = 500 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024
MAX_ACTION_BYTES = 8 * 1024
MAX_RESULT_BYTES = 300
MAX_DESCRIPTION_FIELDS = 20
MAX_ESCAPED_DESCRIPTION_BYTES = 6 * 1024
MAX_APPROVAL_SECTIONS = 3
MAX_LABEL_BYTES = 64
MAX_VALUE_BYTES = 512
MAX_SECTION_TITLE_BYTES = 80
MAX_SECTION_SUMMARY_BYTES = 512
MAX_SECTION_CONTENT_BYTES = 3_000
MAX_APPROVAL_PRESENTATION_BYTES = 3_600
MAX_RENDERED_DIFF_LINES = 120
MAX_PAGE_BYTES = 32 * 1024
MAX_FORM_BYTES = 4 * 1024
MAX_TERMINAL_RECORDS = 1_000
UPLOAD_DEADLINE_SECONDS = 120.0
DECISION_TTL_SECONDS = 24 * 60 * 60
UNCERTAIN_TTL_SECONDS = 24 * 60 * 60
MAINTENANCE_INTERVAL_SECONDS = 5 * 60
MAX_RECONCILES_PER_PASS = 2

REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
RESULT_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
ROUTE_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*\.v[1-9][0-9]*$")
BIDI_CONTROL_RE = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u206f]")
OPEN_STATES = frozenset({"pending", "approved", "executing", "uncertain"})
TERMINAL_STATES = frozenset({"succeeded", "failed", "denied", "expired", "abandoned"})
BODY_DELETE_STATES = TERMINAL_STATES | {"uncertain"}
ALL_STATES = OPEN_STATES | TERMINAL_STATES

LOG = logging.getLogger("external_gate")


class GateError(Exception):
    """Safe request error carrying an HTTP status and fixed result code."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ActionResult:
    state: str
    code: str
    message: str
    restart_before_reconcile: bool = False


@dataclass(frozen=True)
class ApprovalSection:
    """Bounded route-specific content rendered by the trusted generic UI."""

    title: str
    kind: str
    content: str
    summary: str = ""
    truncated: bool = False


class ActionRoute:
    """Small reviewed interface implemented by every production action."""

    name: str
    content_type: str
    max_body_bytes: int
    metadata_headers: tuple[str, ...]
    display_name: str = ""

    def freeze(self, metadata: dict[str, str], body_path: Path, digest: str, size: int):
        raise NotImplementedError

    def decode(self, frozen):
        raise NotImplementedError

    def describe(self, action) -> list[tuple[str, str]]:
        raise NotImplementedError

    def approval_sections(self, action) -> list[ApprovalSection]:
        return []

    def execute(self, action, body_path: Path) -> ActionResult:
        raise NotImplementedError

    def reconcile(self, action) -> ActionResult:
        raise NotImplementedError


@dataclass(frozen=True)
class GateConfig:
    state_dir: Path
    socket_path: Path
    public_origin: str
    password: str
    upload_deadline: float = UPLOAD_DEADLINE_SECONDS
    decision_ttl: float = DECISION_TTL_SECONDS
    uncertain_ttl: float = UNCERTAIN_TTL_SECONDS
    maintenance_interval: float = MAINTENANCE_INTERVAL_SECONDS
    max_terminal_records: int = MAX_TERMINAL_RECORDS


def canonical_json(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def neutralize_bidi_controls(value: str) -> str:
    return BIDI_CONTROL_RE.sub(lambda match: f"\\u{ord(match.group()):04X}", value)


def bounded_text(value: str, limit: int) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 and char not in "\t" for char in value):
        raise ValueError("invalid text")
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    encoded = encoded[:limit]
    while True:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]


class Gate:
    """Owns persistence, approval transitions, quotas, and one worker."""

    def __init__(
        self,
        config: GateConfig,
        routes: dict[str, ActionRoute],
        *,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        fatal_exit: Callable[[], None] | None = None,
    ):
        if not routes or any(name != route.name or not ROUTE_RE.fullmatch(name) for name, route in routes.items()):
            raise ValueError("invalid route registry")
        if len(config.password) < 20:
            raise ValueError("approval password must contain at least 20 characters")
        self.config = config
        self.routes = dict(routes)
        self.requests_dir = config.state_dir / "requests"
        self.wall_clock = wall_clock
        self.monotonic = monotonic
        self.fatal_exit = fatal_exit or (lambda: os._exit(1))
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.reserved_bytes = 0
        self.reserved_slots = 0
        self.csrf_token = secrets.token_urlsafe(32)
        self.stopping = threading.Event()
        self.worker: threading.Thread | None = None
        for directory in (config.state_dir, self.requests_dir, config.socket_path.parent):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        self.recover_startup()

    def _fatal_storage_failure(self) -> None:
        if not self.stopping.is_set():
            self.stopping.set()
            self.fatal_exit()

    def request_path(self, request_id: str) -> Path:
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("invalid request id")
        return self.requests_dir / request_id

    def _validate_record(self, value, expected_id: str | None = None) -> dict:
        keys = {
            "schema_version", "id", "route", "action", "body_size", "body_sha256",
            "state", "created_at", "decision_deadline", "state_changed_at",
            "approved_at", "result",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("invalid request envelope")
        if value["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported request schema")
        request_id = value["id"]
        if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id) or (expected_id and request_id != expected_id):
            raise ValueError("invalid request id")
        if value["route"] not in self.routes:
            raise ValueError("invalid route envelope")
        action_bytes = canonical_json(value["action"])
        if len(action_bytes) > MAX_ACTION_BYTES:
            raise ValueError("frozen action is too large")
        if not isinstance(value["body_size"], int) or isinstance(value["body_size"], bool) or value["body_size"] <= 0:
            raise ValueError("invalid body size")
        digest = value["body_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid body digest")
        if value["state"] not in ALL_STATES:
            raise ValueError("invalid state")
        for field in ("created_at", "decision_deadline", "state_changed_at"):
            if not isinstance(value[field], (int, float)) or isinstance(value[field], bool):
                raise ValueError("invalid timestamp")
        if value["approved_at"] is not None and (
            not isinstance(value["approved_at"], (int, float)) or isinstance(value["approved_at"], bool)
        ):
            raise ValueError("invalid approval timestamp")
        result = value["result"]
        if not isinstance(result, dict) or set(result) != {"code", "message"}:
            raise ValueError("invalid result")
        if not isinstance(result["code"], str) or (result["code"] and not RESULT_CODE_RE.fullmatch(result["code"])):
            raise ValueError("invalid result code")
        if not isinstance(result["message"], str) or len(result["message"].encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError("invalid result message")
        if len(canonical_json(value)) > MAX_RECORD_BYTES:
            raise ValueError("request metadata is too large")
        return value

    def load(self, request_id: str) -> dict:
        path = self.request_path(request_id) / "request.json"
        if path.stat().st_size > MAX_RECORD_BYTES:
            raise ValueError("request metadata is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
        return self._validate_record(value, request_id)

    def _records(self) -> list[dict]:
        records: list[dict] = []
        for entry in self.requests_dir.iterdir():
            if entry.is_dir() and REQUEST_ID_RE.fullmatch(entry.name):
                records.append(self.load(entry.name))
        return records

    def _write_record(self, record: dict) -> None:
        record = self._validate_record(record, record.get("id") if isinstance(record, dict) else None)
        data = canonical_json(record)
        request_dir = self.request_path(record["id"])
        temp = request_dir / f".request.json.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        try:
            os.replace(temp, request_dir / "request.json")
            fsync_dir(request_dir)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()

    def _delete_body(self, request_id: str) -> None:
        request_dir = self.request_path(request_id)
        body = request_dir / "body"
        try:
            body.unlink()
        except FileNotFoundError:
            return
        fsync_dir(request_dir)

    def _log_transition(self, record: dict, old: str, new: str, code: str) -> None:
        LOG.info("transition id=%s route=%s old=%s new=%s code=%s", record["id"], record["route"], old, new, code)

    def transition(
        self,
        request_id: str,
        expected: str | tuple[str, ...],
        new_state: str,
        code: str,
        message: str,
        *,
        approved_at: float | None = None,
    ) -> dict | None:
        expected_states = (expected,) if isinstance(expected, str) else expected
        if new_state not in ALL_STATES or not RESULT_CODE_RE.fullmatch(code):
            raise ValueError("invalid transition")
        with self.condition:
            try:
                record = self.load(request_id)
            except (OSError, ValueError, json.JSONDecodeError):
                self._fatal_storage_failure()
                raise
            if record["state"] not in expected_states:
                return None
            old_state = record["state"]
            record["state"] = new_state
            record["state_changed_at"] = self.wall_clock()
            if approved_at is not None:
                record["approved_at"] = approved_at
            record["result"] = {"code": code, "message": bounded_text(message, MAX_RESULT_BYTES)}
            try:
                self._write_record(record)
                if new_state in BODY_DELETE_STATES:
                    self._delete_body(request_id)
            except OSError:
                self._fatal_storage_failure()
                raise
            self._log_transition(record, old_state, new_state, code)
            if new_state in TERMINAL_STATES:
                try:
                    self._prune_terminal_locked()
                except OSError:
                    self._fatal_storage_failure()
                    raise
            return record

    def _validate_description(self, route: ActionRoute, action) -> list[tuple[str, str]]:
        fields = route.describe(action)
        if not isinstance(fields, list) or len(fields) > MAX_DESCRIPTION_FIELDS:
            raise ValueError("invalid approval description")
        checked: list[tuple[str, str]] = []
        escaped_bytes = 0
        for field in fields:
            if not isinstance(field, tuple) or len(field) != 2:
                raise ValueError("invalid approval description")
            label, value = field
            if not isinstance(label, str) or not isinstance(value, str):
                raise ValueError("invalid approval description")
            if (
                any(ord(char) < 32 for char in label + value)
                or BIDI_CONTROL_RE.search(label + value) is not None
            ):
                raise ValueError("invalid approval description text")
            if len(label.encode("utf-8")) > MAX_LABEL_BYTES or len(value.encode("utf-8")) > MAX_VALUE_BYTES:
                raise ValueError("approval description is too large")
            escaped_bytes += len(html.escape(label).encode("utf-8")) + len(html.escape(value).encode("utf-8"))
            if escaped_bytes > MAX_ESCAPED_DESCRIPTION_BYTES:
                raise ValueError("escaped approval description is too large")
            checked.append((label, value))
        return checked

    def _validate_sections(self, route: ActionRoute, action) -> list[ApprovalSection]:
        sections = route.approval_sections(action)
        if not isinstance(sections, list) or len(sections) > MAX_APPROVAL_SECTIONS:
            raise ValueError("invalid approval sections")
        checked: list[ApprovalSection] = []
        presentation_bytes = 0
        for section in sections:
            if not isinstance(section, ApprovalSection) or section.kind not in {"diff", "code", "text"}:
                raise ValueError("invalid approval section")
            if not isinstance(section.truncated, bool):
                raise ValueError("invalid approval section")
            values = (
                (section.title, MAX_SECTION_TITLE_BYTES, False),
                (section.summary, MAX_SECTION_SUMMARY_BYTES, True),
                (section.content, MAX_SECTION_CONTENT_BYTES, True),
            )
            for value, limit, multiline in values:
                if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
                    raise ValueError("approval section is too large")
                presentation_bytes += len(value.encode("utf-8"))
                if presentation_bytes > MAX_APPROVAL_PRESENTATION_BYTES:
                    raise ValueError("approval section is too large")
                allowed_controls = {"\t", "\n"} if multiline else {"\t"}
                if (
                    any(ord(char) < 32 and char not in allowed_controls for char in value)
                    or BIDI_CONTROL_RE.search(value) is not None
                ):
                    raise ValueError("invalid approval section text")
            checked.append(section)
        return checked

    def _decode(self, record: dict):
        route = self.routes[record["route"]]
        try:
            action = route.decode(record["action"])
        except Exception:
            if record["state"] in TERMINAL_STATES:
                return None
            target = "failed" if record["state"] in {"pending", "approved"} else "abandoned"
            code = "invalid_action" if target == "failed" else "invalid_action_unknown"
            message = "stored action is invalid" if target == "failed" else "stored action is invalid; external outcome is unknown"
            self.transition(record["id"], record["state"], target, code, message)
            return None
        return route, action

    def _prune_terminal_locked(self) -> None:
        terminals = [record for record in self._records() if record["state"] in TERMINAL_STATES]
        terminals.sort(key=lambda record: (record["state_changed_at"], record["id"]))
        changed = False
        for record in terminals[:-self.config.max_terminal_records] if self.config.max_terminal_records else terminals:
            shutil.rmtree(self.request_path(record["id"]))
            changed = True
        if changed:
            fsync_dir(self.requests_dir)

    def _expire_pending_locked(self, now: float) -> None:
        for record in self._records():
            if record["state"] == "pending" and now >= record["decision_deadline"]:
                self.transition(record["id"], "pending", "expired", "decision_expired", "approval deadline expired")

    def recover_startup(self) -> None:
        with self.condition:
            for entry in list(self.requests_dir.iterdir()):
                if entry.name.startswith(".submit-") or entry.name.startswith(".request-"):
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                    continue
                if entry.is_dir() and REQUEST_ID_RE.fullmatch(entry.name):
                    removed = False
                    for temporary in entry.glob(".request.json.*.tmp"):
                        temporary.unlink()
                        removed = True
                    if removed:
                        fsync_dir(entry)
            fsync_dir(self.requests_dir)
            records = self._records()
            for record in records:
                if record["state"] in BODY_DELETE_STATES:
                    self._delete_body(record["id"])
                decoded = self._decode(record)
                if decoded is None:
                    continue
                if record["state"] == "executing":
                    self.transition(
                        record["id"], "executing", "uncertain", "restart_reconcile",
                        "gate restarted during execution; outcome requires reconciliation",
                    )
            self._expire_pending_locked(self.wall_clock())
            self._prune_terminal_locked()

    def _reserve(self, size: int) -> None:
        with self.condition:
            if self.stopping.is_set():
                raise GateError(503, "gate_stopping", "external gate is stopping")
            try:
                self._expire_pending_locked(self.wall_clock())
                self._prune_terminal_locked()
                records = self._records()
                open_records = [record for record in records if record["state"] in OPEN_STATES]
                stored_bytes = 0
                for record in open_records:
                    body = self.request_path(record["id"]) / "body"
                    if body.exists():
                        stored_bytes += record["body_size"]
            except (OSError, ValueError, json.JSONDecodeError) as error:
                self._fatal_storage_failure()
                raise GateError(503, "storage_failure", "local request storage failed") from error
            if len(open_records) + self.reserved_slots >= MAX_OPEN_REQUESTS:
                raise GateError(409, "open_quota", "open request quota reached")
            if stored_bytes + self.reserved_bytes + size > MAX_OPEN_BODY_BYTES:
                raise GateError(409, "body_quota", "open body quota reached")
            self.reserved_slots += 1
            self.reserved_bytes += size

    def _release_reservation(self, size: int) -> None:
        with self.condition:
            self.reserved_slots -= 1
            self.reserved_bytes -= size

    def submit(
        self,
        route_name: str,
        metadata: dict[str, str],
        size: int,
        read_chunk: Callable[[int], bytes],
        deadline: float,
    ) -> dict:
        route = self.routes.get(route_name)
        if route is None:
            raise GateError(404, "unknown_route", "unknown route")
        if size <= 0 or size > route.max_body_bytes:
            raise GateError(413, "body_size", "request body is outside route limit")
        self._reserve(size)
        request_id = secrets.token_hex(16)
        temp_dir = Path(os.path.join(self.requests_dir, f".submit-{request_id}-{secrets.token_hex(4)}"))
        published = False
        try:
            temp_dir.mkdir(mode=0o700)
            body_path = temp_dir / "body"
            digest = hashlib.sha256()
            remaining = size
            descriptor = os.open(body_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as body:
                    while remaining:
                        if self.monotonic() >= deadline:
                            raise GateError(408, "upload_deadline", "upload deadline exceeded")
                        try:
                            chunk = read_chunk(min(1024 * 1024, remaining))
                        except OSError as error:
                            raise GateError(400, "body_read", "request body could not be read") from error
                        if not chunk:
                            raise GateError(400, "short_body", "request body ended early")
                        if len(chunk) > remaining:
                            chunk = chunk[:remaining]
                        body.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if self.monotonic() >= deadline:
                        raise GateError(408, "upload_deadline", "upload deadline exceeded")
                    body.flush()
                    os.fsync(body.fileno())
            finally:
                os.close(descriptor)
            try:
                frozen = route.freeze(metadata, body_path, digest.hexdigest(), size)
                action_data = canonical_json(frozen)
                if len(action_data) > MAX_ACTION_BYTES:
                    raise GateError(413, "action_size", "frozen action is too large")
                action = route.decode(frozen)
                self._validate_description(route, action)
                self._validate_sections(route, action)
            except GateError:
                raise
            except (OSError, ValueError) as error:
                raise GateError(400, "invalid_action", "route action validation failed") from error
            now = self.wall_clock()
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": request_id,
                "route": route_name,
                "action": frozen,
                "body_size": size,
                "body_sha256": digest.hexdigest(),
                "state": "pending",
                "created_at": now,
                "decision_deadline": now + self.config.decision_ttl,
                "state_changed_at": now,
                "approved_at": None,
                "result": {"code": "", "message": ""},
            }
            data = canonical_json(self._validate_record(record, request_id))
            request_json = temp_dir / "request.json"
            meta_fd = os.open(request_json, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(meta_fd, "wb", closefd=False) as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                os.close(meta_fd)
            fsync_dir(temp_dir)
            with self.condition:
                os.replace(temp_dir, self.request_path(request_id))
                fsync_dir(self.requests_dir)
                self.reserved_slots -= 1
                self.reserved_bytes -= size
                published = True
                self._prune_terminal_locked()
            return {
                "id": request_id,
                "approval_url": f"{self.config.public_origin}/r/{request_id}",
            }
        except GateError:
            raise
        except OSError as error:
            self._fatal_storage_failure()
            raise GateError(503, "storage_failure", "local request storage failed") from error
        except ValueError as error:
            raise GateError(400, "invalid_request", "request validation failed") from error
        finally:
            if not published:
                self._release_reservation(size)
                try:
                    shutil.rmtree(temp_dir)
                except FileNotFoundError:
                    pass
                except OSError:
                    self._fatal_storage_failure()
                    raise

    def status(self, request_id: str) -> dict | None:
        with self.lock:
            try:
                record = self.load(request_id)
            except FileNotFoundError:
                return None
            except (OSError, ValueError, json.JSONDecodeError):
                self._fatal_storage_failure()
                return None
            return {
                "id": record["id"],
                "route": record["route"],
                "state": record["state"],
                "result": record["result"]["message"],
            }

    def decide(self, request_id: str, decision: str) -> bool:
        if decision not in {"approve", "deny"}:
            return False
        with self.condition:
            try:
                record = self.load(request_id)
            except FileNotFoundError:
                return False
            except (OSError, ValueError, json.JSONDecodeError):
                self._fatal_storage_failure()
                return False
            if record["state"] != "pending":
                return False
            now = self.wall_clock()
            if now >= record["decision_deadline"]:
                self.transition(request_id, "pending", "expired", "decision_expired", "approval deadline expired")
                return False
            if decision == "deny":
                return self.transition(request_id, "pending", "denied", "denied", "denied by approver") is not None
            approved = self.transition(
                request_id, "pending", "approved", "approved", "approved for one execution attempt", approved_at=now
            )
            if approved is not None:
                self.condition.notify()
                return True
            return False

    def approval_fields(
        self, request_id: str
    ) -> tuple[dict, list[tuple[str, str]], list[ApprovalSection]] | None:
        with self.condition:
            try:
                record = self.load(request_id)
            except FileNotFoundError:
                return None
            except (OSError, ValueError, json.JSONDecodeError):
                self._fatal_storage_failure()
                return None
            decoded = self._decode(record)
            if decoded is None:
                return None
            route, action = decoded
            try:
                fields = self._validate_description(route, action)
                sections = self._validate_sections(route, action)
            except ValueError:
                if record["state"] not in TERMINAL_STATES:
                    target = "failed" if record["state"] in {"pending", "approved"} else "abandoned"
                    self.transition(request_id, record["state"], target, "invalid_description", "route description is invalid")
                return None
            return record, fields, sections

    def _hash_body(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _next_approved(self) -> dict | None:
        with self.condition:
            approved = [record for record in self._records() if record["state"] == "approved"]
            approved.sort(key=lambda record: (record["approved_at"] or record["created_at"], record["id"]))
            if not approved:
                return None
            record = approved[0]
            return self.transition(record["id"], "approved", "executing", "executing", "executing approved action")

    def _validate_result(self, result: ActionResult) -> ActionResult:
        if not isinstance(result, ActionResult) or result.state not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("invalid action result")
        if not RESULT_CODE_RE.fullmatch(result.code):
            raise ValueError("invalid action result code")
        if (
            not isinstance(result.message, str)
            or len(result.message.encode("utf-8")) > MAX_RESULT_BYTES
            or any(ord(char) < 32 and char != "\t" for char in result.message)
        ):
            raise ValueError("invalid action result message")
        message = result.message
        if result.restart_before_reconcile and result.state != "uncertain":
            raise ValueError("restart barrier requires uncertainty")
        return ActionResult(result.state, result.code, message, result.restart_before_reconcile)

    def execute_one(self, record: dict) -> None:
        request_id = record["id"]
        with self.condition:
            current = self.load(request_id)
            decoded = self._decode(current)
        if decoded is None:
            return
        route, action = decoded
        body_path = self.request_path(request_id) / "body"
        try:
            body_stat = body_path.stat()
            if not stat.S_ISREG(body_stat.st_mode) or body_stat.st_size != current["body_size"]:
                raise ValueError("missing body")
            if self._hash_body(body_path) != current["body_sha256"]:
                raise ValueError("body digest mismatch")
        except FileNotFoundError:
            self.transition(request_id, "executing", "failed", "body_invalid", "stored body failed integrity verification")
            return
        except OSError:
            self._fatal_storage_failure()
            raise
        except ValueError:
            self.transition(request_id, "executing", "failed", "body_invalid", "stored body failed integrity verification")
            return
        try:
            result = self._validate_result(route.execute(action, body_path))
        except Exception:
            result = ActionResult("uncertain", "route_error", "route failed with an unknown external outcome")
        if result.restart_before_reconcile:
            try:
                self.transition(request_id, "executing", result.state, result.code, result.message)
            finally:
                self._fatal_storage_failure()
            return
        self.transition(request_id, "executing", result.state, result.code, result.message)

    def reconcile_one(self, record: dict) -> None:
        with self.condition:
            current = self.load(record["id"])
            if current["state"] != "uncertain":
                return
            decoded = self._decode(current)
        if decoded is None:
            return
        route, action = decoded
        try:
            result = self._validate_result(route.reconcile(action))
        except Exception:
            return
        if result.state == "uncertain" or result.restart_before_reconcile:
            return
        self.transition(current["id"], "uncertain", result.state, result.code, result.message)

    def maintenance(self) -> None:
        with self.condition:
            self._expire_pending_locked(self.wall_clock())
            uncertain = [record for record in self._records() if record["state"] == "uncertain"]
            uncertain.sort(key=lambda record: (record["state_changed_at"], record["id"]))
        reconciled = 0
        for record in uncertain:
            with self.condition:
                if any(item["state"] == "approved" for item in self._records()):
                    break
            if reconciled >= MAX_RECONCILES_PER_PASS:
                break
            self.reconcile_one(record)
            reconciled += 1
        now = self.wall_clock()
        with self.condition:
            for record in self._records():
                if record["state"] == "uncertain" and now - record["state_changed_at"] >= self.config.uncertain_ttl:
                    self.transition(
                        record["id"], "uncertain", "abandoned", "uncertainty_abandoned",
                        "external outcome remains unknown; inspect the remote manually",
                    )

    def _worker_loop(self) -> None:
        next_maintenance = self.monotonic()
        try:
            while not self.stopping.is_set():
                record = self._next_approved()
                if record is not None:
                    self.execute_one(record)
                    continue
                now = self.monotonic()
                if now >= next_maintenance:
                    self.maintenance()
                    next_maintenance = now + self.config.maintenance_interval
                    continue
                with self.condition:
                    self.condition.wait(timeout=max(0.0, next_maintenance - self.monotonic()))
        except Exception:
            self._fatal_storage_failure()

    def start_worker(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self._worker_loop, name="external-gate-worker", daemon=True)
        self.worker.start()

    def stop_worker(self) -> None:
        self.stopping.set()
        with self.condition:
            self.condition.notify_all()
        if self.worker:
            self.worker.join(timeout=5)

    def healthy(self) -> bool:
        return (
            not self.stopping.is_set()
            and self.requests_dir.is_dir()
            and os.access(self.requests_dir, os.R_OK | os.W_OK | os.X_OK)
        )


class QuietServerMixin:
    def handle_error(self, _request, _client_address):
        return


class UnixHTTPServer(QuietServerMixin, socketserver.UnixStreamServer):
    allow_reuse_address = True


class BrowserHTTPServer(QuietServerMixin, HTTPServer):
    allow_reuse_address = True


class BoundedHandler(BaseHTTPRequestHandler):
    server_version = "VivariumExternalGate/1"
    request_timeout = 120.0
    idle_timeout = 10.0

    def log_message(self, _format, *_args):
        return

    def handle(self):
        self.connection.settimeout(self.idle_timeout)
        clock = getattr(getattr(self, "gate", None), "monotonic", time.monotonic)
        duration = min(
            self.request_timeout,
            getattr(getattr(getattr(self, "gate", None), "config", None), "upload_deadline", self.request_timeout),
        )
        self.absolute_deadline = clock() + duration
        timer = threading.Timer(duration, self._expire_connection)
        timer.daemon = True
        timer.start()
        try:
            super().handle()
        except (ConnectionError, OSError, socket.timeout):
            pass
        finally:
            timer.cancel()

    def _expire_connection(self):
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)

    def json_response(self, status: int, value: dict) -> None:
        body = canonical_json(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(body)

    def fixed_error(self, error: GateError) -> None:
        self.json_response(error.status, {"error": error.code, "message": error.message})

    def one_header(self, name: str) -> str | None:
        values = self.headers.get_all(name, failobj=[])
        if len(values) != 1:
            return None
        return values[0]


class AgentHandler(BoundedHandler):
    gate: Gate

    def do_GET(self):
        if self.path == "/healthz":
            self.json_response(200 if self.gate.healthy() else 503, {"status": "ok" if self.gate.healthy() else "unhealthy"})
            return
        match = re.fullmatch(r"/v1/requests/([0-9a-f]{32})", self.path)
        if not match:
            self.fixed_error(GateError(404, "not_found", "request not found"))
            return
        status = self.gate.status(match.group(1))
        if status is None:
            self.fixed_error(GateError(404, "not_found", "request not found"))
            return
        self.json_response(200, status)

    def do_POST(self):
        match = re.fullmatch(r"/v1/requests/([a-z0-9.-]+)", self.path)
        if not match:
            self.fixed_error(GateError(404, "not_found", "route not found"))
            return
        route_name = match.group(1)
        route = self.gate.routes.get(route_name)
        if route is None:
            self.fixed_error(GateError(404, "unknown_route", "unknown route"))
            return
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            self.fixed_error(GateError(400, "transfer_encoding", "transfer encoding is not supported"))
            return
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            self.fixed_error(GateError(400, "content_length", "one valid content length is required"))
            return
        content_types = self.headers.get_all("Content-Type", failobj=[])
        if content_types != [route.content_type]:
            self.fixed_error(GateError(415, "content_type", "unsupported content type"))
            return
        size = int(lengths[0])
        declared = {name.lower(): name for name in route.metadata_headers}
        supplied_route_headers = {name.lower() for name in self.headers if name.lower().startswith("x-vivarium-")}
        if supplied_route_headers != set(declared):
            self.fixed_error(GateError(400, "metadata_headers", "route metadata headers do not match the route"))
            return
        metadata: dict[str, str] = {}
        for lower, canonical in declared.items():
            values = self.headers.get_all(canonical, failobj=[])
            if len(values) != 1:
                self.fixed_error(GateError(400, "metadata_headers", "route metadata header must appear exactly once"))
                return
            metadata[canonical] = values[0]
        deadline = self.absolute_deadline

        def read_chunk(amount: int) -> bytes:
            remaining = deadline - self.gate.monotonic()
            if remaining <= 0:
                return b""
            self.connection.settimeout(min(self.idle_timeout, remaining))
            return self.rfile.read(amount)

        try:
            result = self.gate.submit(route_name, metadata, size, read_chunk, deadline)
        except GateError as error:
            self.fixed_error(error)
            return
        self.json_response(201, result)


class BrowserHandler(BoundedHandler):
    gate: Gate
    request_timeout = 10.0
    idle_timeout = 5.0

    def send_response(self, code, message=None):
        super().send_response(code, message)
        nonce = getattr(self, "csp_nonce", "")
        dynamic = f"; script-src 'nonce-{nonce}'; connect-src 'self'" if nonce else ""
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'" + dynamic,
        )
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")

    def authenticated(self) -> bool:
        values = self.headers.get_all("Authorization", failobj=[])
        if len(values) != 1:
            return False
        try:
            scheme, encoded = values[0].split(" ", 1)
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeError):
            return False
        return (
            scheme == "Basic"
            and hmac.compare_digest(username, "vivarium")
            and hmac.compare_digest(password.encode(), self.gate.config.password.encode())
        )

    def require_auth(self) -> bool:
        if self.authenticated():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Vivarium external gate", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if self.path == "/healthz":
            self.json_response(200 if self.gate.healthy() else 503, {"status": "ok" if self.gate.healthy() else "unhealthy"})
            return
        if not self.require_auth():
            return
        status_match = re.fullmatch(r"/r/([0-9a-f]{32})/status", self.path)
        if status_match:
            status = self.gate.status(status_match.group(1))
            if status is None:
                self.fixed_error(GateError(404, "not_found", "request not found"))
                return
            self.json_response(200, status)
            return
        match = re.fullmatch(r"/r/([0-9a-f]{32})", self.path)
        if not match:
            self.fixed_error(GateError(404, "not_found", "request not found"))
            return
        loaded = self.gate.approval_fields(match.group(1))
        if loaded is None:
            self.fixed_error(GateError(404, "not_found", "request not found"))
            return
        record, fields, sections = loaded
        if record["state"] in {"approved", "executing"}:
            self.csp_nonce = secrets.token_urlsafe(18)
        body = self.render_page(record, fields, sections)
        if len(body) > MAX_PAGE_BYTES:
            body = self.render_page(record, fields, [], previews_omitted=True)
        if len(body) > MAX_PAGE_BYTES:
            self.fixed_error(GateError(500, "page_size", "approval page exceeds its limit"))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.require_auth():
            return
        origins = self.headers.get_all("Origin", failobj=[])
        if len(origins) > 1 or (origins and origins[0] != self.gate.config.public_origin):
            self.fixed_error(GateError(403, "origin", "origin does not match approval origin"))
            return
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            self.fixed_error(GateError(400, "transfer_encoding", "transfer encoding is not supported"))
            return
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            self.fixed_error(GateError(400, "content_length", "one valid content length is required"))
            return
        length = int(lengths[0])
        if length <= 0 or length > MAX_FORM_BYTES:
            self.fixed_error(GateError(413, "form_size", "approval form is outside its limit"))
            return
        if self.headers.get_all("Content-Type", failobj=[]) != ["application/x-www-form-urlencoded"]:
            self.fixed_error(GateError(415, "content_type", "unsupported content type"))
            return
        try:
            encoded_form = self.rfile.read(length)
            if len(encoded_form) != length:
                raise ValueError("short form")
            form = parse_qs(encoded_form.decode("utf-8"), strict_parsing=True)
        except (UnicodeError, ValueError):
            self.fixed_error(GateError(400, "form", "invalid approval form"))
            return
        tokens = form.get("csrf", [])
        if len(tokens) != 1 or not hmac.compare_digest(tokens[0], self.gate.csrf_token):
            self.fixed_error(GateError(403, "csrf", "invalid CSRF token"))
            return
        match = re.fullmatch(r"/r/([0-9a-f]{32})/(approve|deny)", self.path)
        if not match or not self.gate.decide(match.group(1), match.group(2)):
            self.fixed_error(GateError(409, "decision", "request is no longer pending"))
            return
        self.send_response(303)
        self.send_header("Location", f"/r/{match.group(1)}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    @staticmethod
    def _format_timestamp(value: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(value))

    @staticmethod
    def _route_name(route: ActionRoute) -> str:
        name = route.display_name.strip() if isinstance(route.display_name, str) else ""
        if name and len(name.encode("utf-8")) <= MAX_SECTION_TITLE_BYTES and not any(ord(char) < 32 for char in name):
            return name
        stem = re.sub(r"\.v[1-9][0-9]*$", "", route.name)
        return " ".join(part.capitalize() for part in re.split(r"[.-]", stem))

    @staticmethod
    def _render_diff(content: str) -> str:
        if not content:
            return '<div class="empty-preview">No textual file changes in this request.</div>'
        rows: list[str] = []
        old_line: int | None = None
        new_line: int | None = None
        lines = content.splitlines()
        omitted = len(lines) > MAX_RENDERED_DIFF_LINES
        for line in lines[:MAX_RENDERED_DIFF_LINES]:
            old_number = ""
            new_number = ""
            kind = "context"
            if line.startswith("@@"):
                match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                if match:
                    old_line, new_line = int(match.group(1)), int(match.group(2))
                kind = "hunk"
            elif line.startswith("diff --git "):
                old_line = new_line = None
                kind = "file"
            elif line.startswith(("index ", "--- ", "+++ ", "new file mode ", "deleted file mode ", "similarity index ", "rename from ", "rename to ")):
                kind = "meta"
            elif line.startswith("+"):
                kind = "addition"
                if new_line is not None:
                    new_number = str(new_line)
                    new_line += 1
            elif line.startswith("-"):
                kind = "deletion"
                if old_line is not None:
                    old_number = str(old_line)
                    old_line += 1
            elif line.startswith(" "):
                if old_line is not None and new_line is not None:
                    old_number, new_number = str(old_line), str(new_line)
                    old_line += 1
                    new_line += 1
            elif line.startswith("\\"):
                kind = "meta"
            rows.append(
                f'<tr class="{kind}"><td class="line-number">{old_number}</td>'
                f'<td class="line-number">{new_number}</td><td class="diff-code"><code>{html.escape(line)}</code></td></tr>'
            )
        if omitted:
            rows.append(
                '<tr class="meta"><td class="line-number"></td><td class="line-number"></td>'
                '<td class="diff-code"><code>… additional preview lines omitted</code></td></tr>'
            )
        headings = (
            '<thead class="visually-hidden"><tr><th scope="col">Old line</th>'
            '<th scope="col">New line</th><th scope="col">Code</th></tr></thead>'
        )
        return '<div class="diff-scroll"><table class="diff-table" aria-label="Git diff">' + headings + '<tbody>' + "".join(rows) + "</tbody></table></div>"

    def _render_section(self, section: ApprovalSection) -> str:
        summary = f'<p class="section-summary">{html.escape(section.summary)}</p>' if section.summary else ""
        truncated = '<span class="preview-note">Preview truncated</span>' if section.truncated else ""
        if section.kind == "diff":
            content = self._render_diff(section.content)
        elif section.kind == "code":
            content = f'<pre class="code-block"><code>{html.escape(section.content)}</code></pre>'
        else:
            content = f'<p class="text-preview">{html.escape(section.content)}</p>'
        return (
            '<section class="card preview-card"><div class="section-heading"><div>'
            f'<p class="eyebrow">Request preview</p><h2>{html.escape(section.title)}</h2>{summary}'
            f'</div>{truncated}</div>{content}</section>'
        )

    def render_page(
        self,
        record: dict,
        fields: list[tuple[str, str]],
        sections: list[ApprovalSection],
        *,
        previews_omitted: bool = False,
    ) -> bytes:
        state = record["state"]
        state_copy = {
            "pending": "Review the immutable request below before authorizing one external write attempt.",
            "approved": "Approval recorded. The request is queued for its single execution attempt.",
            "executing": "The approved action is executing now. This page refreshes automatically.",
            "succeeded": "The external action completed and its outcome was confirmed.",
            "failed": "The action stopped without a confirmed successful external write.",
            "denied": "This request was denied and cannot be executed.",
            "expired": "The approval window closed before a decision was recorded.",
            "uncertain": "The external outcome could not be proven. Do not submit a duplicate action.",
            "abandoned": "This uncertain request can no longer be reconciled automatically.",
        }
        state_label = state.replace("_", " ").title()
        route = self.gate.routes[record["route"]]
        title = self._route_name(route)
        active = state in {"approved", "executing"}
        rows = list(fields) + [
            ("Request ID", record["id"]),
            ("Bundle SHA-256", record["body_sha256"]),
            ("Created", self._format_timestamp(record["created_at"])),
            ("Approval deadline", self._format_timestamp(record["decision_deadline"])),
        ]
        details = "".join(
            '<div class="meta-item">'
            f'<dt>{html.escape(label)}</dt><dd><code>{html.escape(value)}</code></dd></div>'
            for label, value in rows
        )
        previews = "".join(self._render_section(section) for section in sections)
        if previews_omitted:
            previews = (
                '<section class="card preview-card"><p class="eyebrow">Request preview</p>'
                '<h2>Preview omitted</h2><p class="section-summary">The optional preview exceeded the bounded page size. '
                'Review the complete immutable request metadata above before deciding.</p></section>'
            )
        result = ""
        if record["result"]["message"]:
            result = (
                f'<section class="card result-card result-{state}"><p class="eyebrow">Current outcome</p>'
                f'<h2>{html.escape(state_label)}</h2><p class="result-message">{html.escape(record["result"]["message"])}</p>'
                f'<div class="result-meta"><span>Result code</span><code>{html.escape(record["result"]["code"])}</code></div></section>'
            )
        decision = ""
        if state == "pending":
            csrf = html.escape(self.gate.csrf_token)
            decision = (
                '<section class="card decision-card"><p class="eyebrow">Decision required</p>'
                '<h2>Authorize this exact action?</h2>'
                '<p class="decision-copy">Approval is immutable and permits at most one external write attempt. It cannot be reused.</p>'
                '<div class="actions">'
                f'<form method="post" action="/r/{record["id"]}/approve"><input type="hidden" name="csrf" value="{csrf}"><button class="approve" type="submit"><span>Approve once</span><small>Execute exact request</small></button></form>'
                f'<form method="post" action="/r/{record["id"]}/deny"><input type="hidden" name="csrf" value="{csrf}"><button class="deny" type="submit"><span>Deny request</span><small>Permanent decision</small></button></form>'
                '</div></section>'
            )
        auto_hint = '<p class="refresh-hint"><span class="spinner" aria-hidden="true"></span>Watching for the durable result</p>' if active else ""
        poller = ""
        if active:
            nonce = html.escape(self.csp_nonce, quote=True)
            poller = f'''<script nonce="{nonce}">(()=>{{const initial=document.body.dataset.state;const poll=async()=>{{try{{const response=await fetch(location.pathname+"/status",{{cache:"no-store",headers:{{Accept:"application/json"}}}});if(response.ok){{const status=await response.json();if(status.state!==initial){{location.reload();return}}}}}}catch(_error){{}}setTimeout(poll,1200)}};setTimeout(poll,800)}})()</script>'''
        return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · Vivarium</title>
<style>
:root{{color-scheme:dark;--bg:#080a0f;--surface:#11151d;--surface-2:#171c26;--border:#293140;--border-soft:#202735;--text:#f4f7fb;--muted:#a6b1c0;--subtle:#98a5b7;--accent:#8ca6ff;--green:#48dda0;--red:#ff7180;--amber:#f4bd56;--blue:#6aabff;--violet:#b596ff;--orange:#ff9862;--radius:18px;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;background:radial-gradient(circle at 50% -12rem,#202a4c 0,transparent 38rem),linear-gradient(180deg,#0b0e15 0,var(--bg) 40rem);color:var(--text);font-size:15px;line-height:1.55}}body:before{{content:"";position:fixed;inset:0;pointer-events:none;opacity:.18;background-image:linear-gradient(#fff1 1px,transparent 1px),linear-gradient(90deg,#fff1 1px,transparent 1px);background-size:48px 48px;mask-image:linear-gradient(to bottom,#000,transparent 42%)}}.shell{{position:relative;width:min(calc(100% - 32px),1040px);margin:0 auto;padding:42px 0 64px}}.brand{{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:13px;font-weight:650;letter-spacing:.02em}}.brand-mark{{display:grid;place-items:center;width:26px;height:26px;border:1px solid #ffffff24;border-radius:8px;background:linear-gradient(145deg,#b9c7ff,#738cff);color:#10131b;font-size:12px;font-weight:900;box-shadow:0 8px 28px #7088ff45}}.brand strong{{color:var(--text)}}.brand-divider{{width:1px;height:15px;background:var(--border)}}.hero{{padding:48px 0 20px}}.hero-top{{display:flex;align-items:flex-start;justify-content:space-between;gap:28px}}.eyebrow{{margin:0 0 8px;color:var(--muted);font-size:11px;font-weight:800;letter-spacing:.13em;text-transform:uppercase}}h1{{max-width:18ch;margin:0;font-size:clamp(34px,6vw,62px);font-weight:760;letter-spacing:-.052em;line-height:1.02}}.hero-copy{{max-width:680px;margin:18px 0 0;color:#b6c0ce;font-size:17px}}.state-pill{{display:inline-flex;align-items:center;gap:9px;flex:none;margin-top:3px;padding:8px 12px;border:1px solid currentColor;border-radius:999px;background:#11151dcc;font-size:11px;font-weight:850;letter-spacing:.08em;text-transform:uppercase;box-shadow:0 10px 32px #0005}}.state-dot{{width:7px;height:7px;border-radius:50%;background:currentColor;box-shadow:0 0 12px currentColor}}.state-pending{{color:var(--amber)}}.state-approved{{color:var(--blue)}}.state-executing{{color:var(--violet)}}.state-succeeded{{color:var(--green)}}.state-failed,.state-denied{{color:var(--red)}}.state-uncertain{{color:var(--orange)}}.state-expired,.state-abandoned{{color:var(--muted)}}.refresh-hint{{display:flex;align-items:center;gap:8px;margin:16px 0 0;color:var(--muted);font-size:13px}}.spinner{{width:13px;height:13px;border:2px solid #ffffff30;border-top-color:var(--violet);border-radius:50%;animation:spin .9s linear infinite}}@keyframes spin{{to{{transform:rotate(360deg)}}}}.card{{margin-top:16px;padding:clamp(18px,3vw,26px);background:linear-gradient(145deg,#141923f2,#0f131bf2);border:1px solid var(--border);border-radius:var(--radius);box-shadow:0 18px 50px #0003,0 1px 0 #ffffff08 inset}}.card h2{{margin:0;font-size:20px;letter-spacing:-.025em}}.meta-card{{padding:12px}}.meta-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1px;margin:0;overflow:hidden;border:1px solid var(--border-soft);border-radius:13px;background:var(--border-soft)}}.meta-item{{min-width:0;padding:15px 16px;background:var(--surface)}}dt{{color:var(--subtle);font-size:10px;font-weight:800;letter-spacing:.09em;text-transform:uppercase}}dd{{margin:6px 0 0;overflow-wrap:anywhere;color:#dce3ec;font-weight:600}}code,pre{{font-family:"SFMono-Regular",Consolas,"Liberation Mono",monospace}}dd code{{font-size:12px}}.section-heading{{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;margin-bottom:18px}}.section-summary{{max-width:700px;margin:7px 0 0;color:var(--muted);font:12px/1.55 "SFMono-Regular",Consolas,monospace;white-space:pre-wrap}}.preview-note{{flex:none;padding:5px 8px;border:1px solid #765b2a;border-radius:999px;color:#efc777;background:#332813;font-size:10px;font-weight:800;text-transform:uppercase}}.code-block,.text-preview{{max-height:260px;margin:0;padding:15px;overflow:auto;border:1px solid var(--border-soft);border-radius:12px;background:#090c12;color:#cdd6e3;font-size:12px;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere}}.text-preview{{font-family:inherit}}.diff-scroll{{overflow:auto;border:1px solid var(--border-soft);border-radius:12px;background:#090c12}}.diff-table{{width:100%;border-collapse:collapse;font:12px/1.55 "SFMono-Regular",Consolas,monospace;white-space:pre}}.diff-table td{{padding-top:1px;padding-bottom:1px}}.line-number{{width:1%;min-width:46px;padding:0 9px;color:#8d9aac;text-align:right;vertical-align:top;user-select:none;border-right:1px solid #ffffff0c}}.diff-code{{padding:0 14px;color:#cbd4e1}}.diff-table .addition{{background:#17362699}}.diff-table .addition .diff-code{{color:#b7f5d1}}.diff-table .deletion{{background:#45212999}}.diff-table .deletion .diff-code{{color:#ffc0c7}}.diff-table .hunk{{background:#172b46}}.diff-table .hunk .diff-code{{color:#8fc2ff}}.diff-table .file{{background:#171c27}}.diff-table .file .diff-code{{padding-top:8px;color:#d6c2ff;font-weight:700}}.diff-table .meta .diff-code{{color:#7f8da0}}.empty-preview{{display:grid;place-items:center;min-height:112px;border:1px dashed #344052;border-radius:12px;color:var(--muted);background:#0a0d13}}.decision-card{{border-color:#46577d;background:linear-gradient(145deg,#182034f2,#111725f2)}}.decision-copy{{max-width:650px;margin:8px 0 0;color:var(--muted)}}.actions{{display:flex;flex-wrap:wrap;gap:12px;margin-top:22px}}form{{margin:0}}button{{display:flex;align-items:flex-start;flex-direction:column;gap:2px;min-width:190px;min-height:58px;padding:10px 16px;border:1px solid transparent;border-radius:12px;font:inherit;cursor:pointer;transition:transform .15s ease,filter .15s ease,border-color .15s ease}}button span{{font-size:14px;font-weight:800}}button small{{font-size:11px;opacity:.7}}button:hover{{transform:translateY(-1px);filter:brightness(1.08)}}button:active{{transform:translateY(0)}}button:focus-visible{{outline:3px solid #9db2ff;outline-offset:3px}}.approve{{background:linear-gradient(135deg,#f6f8fc,#cfd8e8);color:#10131a;box-shadow:0 8px 24px #dbe5ff25}}.deny{{border-color:#663540;background:#28161b;color:#ffadb6}}.result-card{{position:relative;overflow:hidden}}.result-card:before{{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--muted)}}.result-succeeded:before{{background:var(--green)}}.result-failed:before,.result-denied:before{{background:var(--red)}}.result-uncertain:before{{background:var(--orange)}}.result-executing:before{{background:var(--violet)}}.result-message{{margin:8px 0 0;color:#c3ccd9;font-size:16px}}.result-meta{{display:flex;gap:10px;align-items:center;margin-top:18px;color:var(--subtle);font-size:11px;text-transform:uppercase;letter-spacing:.08em}}.result-meta code{{padding:4px 7px;border-radius:6px;background:#090c12;color:#aeb9c9;text-transform:none;letter-spacing:0}}.visually-hidden{{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}}.footer{{display:flex;justify-content:space-between;gap:20px;margin-top:22px;padding:0 4px;color:#8c98a9;font-size:11px}}@media(max-width:660px){{.shell{{width:min(calc(100% - 18px),1040px);padding-top:24px}}.hero{{padding-top:34px}}.hero-top{{display:block}}.state-pill{{margin-top:20px}}.meta-grid{{grid-template-columns:1fr}}.section-heading{{display:block}}.preview-note{{display:inline-flex;margin-top:10px}}.actions,.actions form,button{{width:100%}}button{{min-width:0}}.footer{{display:block}}}}@media(prefers-reduced-motion:reduce){{*{{scroll-behavior:auto!important;transition:none!important}}.spinner{{animation:none}}}}
</style></head><body data-state="{state}"><main class="shell"><header class="brand"><span class="brand-mark">V</span><strong>Vivarium</strong><span class="brand-divider"></span><span>External Action Gate</span></header><section class="hero"><div class="hero-top"><div><p class="eyebrow">One-time external action</p><h1>{html.escape(title)}</h1><p class="hero-copy">{html.escape(state_copy[state])}</p>{auto_hint}</div><span class="state-pill state-{state}" role="status" aria-live="polite"><span class="state-dot"></span>{html.escape(state_label)}</span></div></section><section class="card meta-card"><dl class="meta-grid">{details}</dl></section>{previews}{result}{decision}<footer class="footer"><span>Immutable request · bounded execution · one-shot approval</span><span>{html.escape(record["route"])}</span></footer></main>{poller}</body></html>'''.encode("utf-8")
