"""Bounded, immutable unified-diff artifacts and trusted HTML rendering.

The builder accepts a stream so a rejected large file does not consume the
remaining preview budget or prevent later files from being considered.  The
serialized artifact is canonical JSON; ``load_preview`` is the only supported
way to turn it back into immutable application data.
"""

from __future__ import annotations

import difflib
import html
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

SCHEMA_VERSION = 1
MAX_TOTAL_BYTES = 1024 * 1024
MAX_TOTAL_LINES = 20_000
MAX_FILES = 300
MAX_EXTRA_FILE_COUNT = 1_000_000
MAX_FILE_BYTES = 500 * 1024
MAX_FILE_LINES = 20_000
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_PATH_BYTES = 4 * 1024
MAX_METADATA_FIELDS = 32
MAX_METADATA_KEY_BYTES = 64
MAX_METADATA_VALUE_BYTES = 1024
MAX_INTRALINE_CHARS = 4096
MAX_INTRALINE_TOKENS = 128
MAX_INTRALINE_PAIRS = 100
DEFAULT_HTML_BUDGET = 64 * 1024

OMITTED_BINARY = "binary"
OMITTED_FILE_BYTES = "per_file_bytes"
OMITTED_FILE_LINES = "per_file_lines"
OMITTED_TOTAL_BYTES = "aggregate_bytes"
OMITTED_TOTAL_LINES = "aggregate_lines"
OMISSION_REASONS = frozenset(
    {
        OMITTED_BINARY,
        OMITTED_FILE_BYTES,
        OMITTED_FILE_LINES,
        OMITTED_TOTAL_BYTES,
        OMITTED_TOTAL_LINES,
    }
)
STATUSES = frozenset({"modified", "created", "deleted", "renamed", "copied"})
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]", re.UNICODE)
_BIDI_CLASSES = frozenset({"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"})
_ARTIFACT_KEYS = frozenset({"schema_version", "metadata", "files", "extra_file_count"})
_FILE_KEYS = frozenset(
    {
        "path",
        "old_path",
        "status",
        "binary",
        "metadata",
        "byte_count",
        "line_count",
        "additions",
        "deletions",
        "omission_reason",
        "patch",
    }
)


@dataclass(frozen=True)
class FilePatch:
    """One unified patch and its route-provided file metadata."""

    path: str
    patch: bytes | str
    old_path: str | None = None
    status: str = "modified"
    binary: bool = False
    metadata: Mapping[str, str] | tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class OmittedFilePatch:
    """A streamed file summary whose content was deliberately not retained."""

    path: str
    omission_reason: str
    byte_count: int
    line_count: int
    additions: int = 0
    deletions: int = 0
    old_path: str | None = None
    status: str = "modified"
    binary: bool = False
    metadata: Mapping[str, str] | tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PreviewFile:
    path: str
    old_path: str | None
    status: str
    binary: bool
    metadata: tuple[tuple[str, str], ...]
    byte_count: int
    line_count: int
    additions: int
    deletions: int
    omission_reason: str | None
    patch: str | None


@dataclass(frozen=True)
class DiffPreview:
    schema_version: int
    metadata: tuple[tuple[str, str], ...]
    files: tuple[PreviewFile, ...]
    extra_file_count: int


@dataclass(frozen=True)
class PreviewSelection:
    """Zero-based file selection and one-based page selection."""

    file_index: int = 0
    page: int = 1


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ValueError("value is not canonical JSON") from error


def _safe_text(value: bytes | str, *, multiline: bool) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", "replace")
    elif isinstance(value, str):
        text = value
    else:
        raise ValueError("text must be bytes or str")
    safe: list[str] = []
    for character in text:
        codepoint = ord(character)
        category = unicodedata.category(character)
        if unicodedata.bidirectional(character) in _BIDI_CLASSES or codepoint in {0x061C, 0x200E, 0x200F}:
            safe.append(f"\\u{codepoint:04X}")
        elif character == "\t" or (multiline and character == "\n"):
            safe.append(character)
        elif category in {"Cc", "Cf", "Cs"}:
            safe.append("�")
        else:
            safe.append(character)
    return "".join(safe)


def _bounded_safe_text(value: bytes | str, limit: int, *, multiline: bool, label: str) -> str:
    safe = _safe_text(value, multiline=multiline)
    if len(safe.encode("utf-8")) > limit:
        raise ValueError(f"{label} is too large")
    return safe


def _metadata(value: Mapping[str, str] | tuple[tuple[str, str], ...] | None) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        items = list(value.items())
    elif isinstance(value, (tuple, list)):
        items = list(value)
    else:
        raise ValueError("metadata must be a mapping or pairs")
    if len(items) > MAX_METADATA_FIELDS:
        raise ValueError("too many metadata fields")
    checked: dict[str, str] = {}
    for item in items:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError("invalid metadata pair")
        key = _bounded_safe_text(item[0], MAX_METADATA_KEY_BYTES, multiline=False, label="metadata key")
        entry = _bounded_safe_text(item[1], MAX_METADATA_VALUE_BYTES, multiline=False, label="metadata value")
        if not key or key in checked:
            raise ValueError("invalid or duplicate metadata key")
        checked[key] = entry
    return tuple(sorted(checked.items()))


def _coerce_file(value: FilePatch | OmittedFilePatch | Mapping[str, Any] | tuple[Any, Any]) -> FilePatch | OmittedFilePatch:
    if isinstance(value, (FilePatch, OmittedFilePatch)):
        return value
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], Mapping):
        fields = dict(value[0])
        fields["patch"] = value[1]
    elif isinstance(value, Mapping):
        fields = dict(value)
    else:
        raise ValueError("each file must be FilePatch, a mapping, or (metadata, patch)")
    allowed = {"path", "patch", "old_path", "status", "binary", "metadata"}
    if not {"path", "patch"}.issubset(fields) or not set(fields).issubset(allowed):
        raise ValueError("invalid file fields")
    return FilePatch(**fields)


def _patch_stats(patch: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("@@"):
            in_hunk = _HUNK_RE.match(line) is not None
        elif in_hunk and line.startswith("+"):
            additions += 1
        elif in_hunk and line.startswith("-"):
            deletions += 1
    return additions, deletions


def _infer_file(file: FilePatch, patch: str) -> tuple[str, bool]:
    status = file.status
    if not isinstance(status, str) or status not in STATUSES:
        raise ValueError("invalid file status")
    lines = patch.splitlines()
    binary = file.binary or any(
        line.startswith("Binary files ") or line.startswith("GIT binary patch") for line in lines
    )
    if status == "modified":
        if any(line.startswith("new file mode ") for line in lines) or "--- /dev/null" in lines:
            status = "created"
        elif any(line.startswith("deleted file mode ") for line in lines) or "+++ /dev/null" in lines:
            status = "deleted"
        elif any(line.startswith("rename from ") for line in lines):
            status = "renamed"
    return status, binary


def _file_dict(file: PreviewFile) -> dict[str, Any]:
    return {
        "path": file.path,
        "old_path": file.old_path,
        "status": file.status,
        "binary": file.binary,
        "metadata": dict(file.metadata),
        "byte_count": file.byte_count,
        "line_count": file.line_count,
        "additions": file.additions,
        "deletions": file.deletions,
        "omission_reason": file.omission_reason,
        "patch": file.patch,
    }


def build_preview(
    files: Iterable[FilePatch | OmittedFilePatch | Mapping[str, Any] | tuple[Any, Any]],
    *,
    metadata: Mapping[str, str] | tuple[tuple[str, str], ...] | None = None,
    extra_file_count: int = 0,
) -> bytes:
    """Build a deterministic canonical-JSON artifact from a file stream.

    At most 300 file summaries are accepted. Callers that stopped a larger
    upstream stream at that bound can pass ``extra_file_count``. Content that
    violates a per-file or aggregate limit is omitted without spending aggregate
    budget, so a later small file can still be visible.
    """

    if isinstance(files, (bytes, str, Mapping)) or not isinstance(files, Iterable):
        raise ValueError("files must be an iterable of per-file patches")
    preview_metadata = _metadata(metadata)
    if (
        not isinstance(extra_file_count, int)
        or isinstance(extra_file_count, bool)
        or not 0 <= extra_file_count <= MAX_EXTRA_FILE_COUNT
    ):
        raise ValueError("extra file count is outside its limit")
    retained: list[PreviewFile] = []
    included_bytes = 0
    included_lines = 0

    for raw_file in files:
        if len(retained) >= MAX_FILES:
            raise ValueError("file list exceeds the 300-file input limit")
        source = _coerce_file(raw_file)
        path = _bounded_safe_text(source.path, MAX_PATH_BYTES, multiline=False, label="path")
        if not path:
            raise ValueError("path must not be empty")
        old_path = None
        if source.old_path is not None:
            old_path = _bounded_safe_text(source.old_path, MAX_PATH_BYTES, multiline=False, label="old path")
            if not old_path:
                raise ValueError("old path must not be empty")
        if isinstance(source, OmittedFilePatch):
            if not isinstance(source.status, str) or source.status not in STATUSES or not isinstance(source.binary, bool):
                raise ValueError("invalid omitted file metadata")
            for count in (source.byte_count, source.line_count, source.additions, source.deletions):
                _exact_int(count)
            if source.additions + source.deletions > source.line_count:
                raise ValueError("invalid omitted file statistics")
            patch = None
            byte_count = source.byte_count
            line_count = source.line_count
            additions = source.additions
            deletions = source.deletions
            status = source.status
            binary = source.binary
            expected_reason: str | None = None
            if binary:
                expected_reason = OMITTED_BINARY
            elif byte_count > MAX_FILE_BYTES:
                expected_reason = OMITTED_FILE_BYTES
            elif line_count > MAX_FILE_LINES:
                expected_reason = OMITTED_FILE_LINES
            elif included_bytes + byte_count > MAX_TOTAL_BYTES:
                expected_reason = OMITTED_TOTAL_BYTES
            elif included_lines + line_count > MAX_TOTAL_LINES:
                expected_reason = OMITTED_TOTAL_LINES
            if source.omission_reason != expected_reason or expected_reason is None:
                raise ValueError("inconsistent streamed omission reason")
            reason = expected_reason
        else:
            patch = _safe_text(source.patch, multiline=True).replace("\r\n", "\n").replace("\r", "\n")
            byte_count = len(patch.encode("utf-8"))
            line_count = len(patch.splitlines())
            additions, deletions = _patch_stats(patch)
            status, binary = _infer_file(source, patch)
            reason = None
            if binary:
                reason = OMITTED_BINARY
            elif byte_count > MAX_FILE_BYTES:
                reason = OMITTED_FILE_BYTES
            elif line_count > MAX_FILE_LINES:
                reason = OMITTED_FILE_LINES
            elif included_bytes + byte_count > MAX_TOTAL_BYTES:
                reason = OMITTED_TOTAL_BYTES
            elif included_lines + line_count > MAX_TOTAL_LINES:
                reason = OMITTED_TOTAL_LINES
            if reason is None:
                included_bytes += byte_count
                included_lines += line_count
        retained.append(
            PreviewFile(
                path=path,
                old_path=old_path,
                status=status,
                binary=binary,
                metadata=_metadata(source.metadata),
                byte_count=byte_count,
                line_count=line_count,
                additions=additions,
                deletions=deletions,
                omission_reason=reason,
                patch=None if reason else patch,
            )
        )

    if extra_file_count and len(retained) != MAX_FILES:
        raise ValueError("extra file count requires a full retained file list")
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "metadata": dict(preview_metadata),
        "files": [_file_dict(file) for file in retained],
        "extra_file_count": extra_file_count,
    }
    data = _canonical_json(artifact)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError("diff artifact is too large")
    return data


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _exact_int(value: Any, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError("invalid integer")
    return value


def _load_metadata(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        raise ValueError("invalid metadata")
    checked = _metadata(value)
    if dict(checked) != value:
        raise ValueError("metadata is not normalized")
    return checked


def load_preview(artifact: bytes | str) -> DiffPreview:
    """Strictly validate canonical JSON and return an immutable preview."""

    if isinstance(artifact, str):
        try:
            data = artifact.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("invalid artifact encoding") from error
    elif isinstance(artifact, bytes):
        data = artifact
    else:
        raise ValueError("artifact must be bytes or str")
    if not data or len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError("invalid artifact size")
    try:
        text = data.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_pairs_without_duplicates)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid diff artifact") from error
    if not isinstance(value, dict) or set(value) != _ARTIFACT_KEYS:
        raise ValueError("invalid artifact envelope")
    if (
        not isinstance(value["schema_version"], int)
        or isinstance(value["schema_version"], bool)
        or value["schema_version"] != SCHEMA_VERSION
    ):
        raise ValueError("unsupported diff artifact schema")
    if _canonical_json(value) != data:
        raise ValueError("artifact is not canonical JSON")
    metadata = _load_metadata(value["metadata"])
    extra_file_count = _exact_int(value["extra_file_count"])
    if extra_file_count > MAX_EXTRA_FILE_COUNT:
        raise ValueError("extra file count is too large")
    raw_files = value["files"]
    if not isinstance(raw_files, list) or len(raw_files) > MAX_FILES:
        raise ValueError("invalid file list")
    if extra_file_count and len(raw_files) != MAX_FILES:
        raise ValueError("inconsistent extra file count")

    checked_files: list[PreviewFile] = []
    included_bytes = 0
    included_lines = 0
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != _FILE_KEYS:
            raise ValueError("invalid file summary")
        path = _bounded_safe_text(raw["path"], MAX_PATH_BYTES, multiline=False, label="path")
        if not path or path != raw["path"]:
            raise ValueError("invalid path")
        old_path = raw["old_path"]
        if old_path is not None:
            checked_old = _bounded_safe_text(old_path, MAX_PATH_BYTES, multiline=False, label="old path")
            if not checked_old or checked_old != old_path:
                raise ValueError("invalid old path")
        status = raw["status"]
        binary = raw["binary"]
        if not isinstance(status, str) or status not in STATUSES or not isinstance(binary, bool):
            raise ValueError("invalid file metadata")
        file_metadata = _load_metadata(raw["metadata"])
        byte_count = _exact_int(raw["byte_count"])
        line_count = _exact_int(raw["line_count"])
        additions = _exact_int(raw["additions"])
        deletions = _exact_int(raw["deletions"])
        if additions + deletions > line_count or (byte_count == 0) != (line_count == 0):
            raise ValueError("impossible file summary")
        reason = raw["omission_reason"]
        patch = raw["patch"]
        if reason is not None and (not isinstance(reason, str) or reason not in OMISSION_REASONS):
            raise ValueError("invalid omission reason")
        if patch is not None:
            if not isinstance(patch, str) or reason is not None:
                raise ValueError("invalid retained patch")
            if _safe_text(patch, multiline=True) != patch or "\r" in patch:
                raise ValueError("unsafe retained patch")
            if len(patch.encode("utf-8")) != byte_count or len(patch.splitlines()) != line_count:
                raise ValueError("patch summary mismatch")
            if _patch_stats(patch) != (additions, deletions):
                raise ValueError("patch statistics mismatch")
            inferred_status, inferred_binary = _infer_file(
                FilePatch(path, patch, old_path, status, binary, file_metadata), patch
            )
            if inferred_status != status or inferred_binary != binary:
                raise ValueError("patch metadata mismatch")
        elif reason is None:
            raise ValueError("missing retained patch")

        expected_reason: str | None = None
        if binary:
            expected_reason = OMITTED_BINARY
        elif byte_count > MAX_FILE_BYTES:
            expected_reason = OMITTED_FILE_BYTES
        elif line_count > MAX_FILE_LINES:
            expected_reason = OMITTED_FILE_LINES
        elif included_bytes + byte_count > MAX_TOTAL_BYTES:
            expected_reason = OMITTED_TOTAL_BYTES
        elif included_lines + line_count > MAX_TOTAL_LINES:
            expected_reason = OMITTED_TOTAL_LINES
        if reason != expected_reason:
            raise ValueError("inconsistent omission reason")
        if reason is None:
            included_bytes += byte_count
            included_lines += line_count
        checked_files.append(
            PreviewFile(
                path,
                old_path,
                status,
                binary,
                file_metadata,
                byte_count,
                line_count,
                additions,
                deletions,
                reason,
                patch,
            )
        )
    return DiffPreview(SCHEMA_VERSION, metadata, tuple(checked_files), extra_file_count)


def _query_value(query: Mapping[str, Any], name: str) -> str | None:
    value = query.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and len(value) == 1 and isinstance(value[0], str):
        return value[0]
    return None


def _selection(
    preview: DiffPreview,
    selection: PreviewSelection | None,
    file: int | str | None,
    page: int | None,
    query: str | Mapping[str, Any] | None,
) -> PreviewSelection:
    index = selection.file_index if selection else 0
    selected_page = selection.page if selection else 1
    if query is not None:
        if isinstance(query, str):
            try:
                parsed: Mapping[str, Any] = parse_qs(query.lstrip("?"), keep_blank_values=True, strict_parsing=True)
            except ValueError as error:
                raise ValueError("invalid preview query") from error
        elif isinstance(query, Mapping):
            parsed = query
        else:
            raise ValueError("query must be a string or mapping")
        if not set(parsed).issubset({"file", "file_index", "page"}):
            raise ValueError("invalid preview query parameter")
        file = _query_value(parsed, "file")
        raw_index = _query_value(parsed, "file_index")
        raw_page = _query_value(parsed, "page")
        if "file" in parsed and file is None:
            raise ValueError("invalid file selection")
        if "file" in parsed and "file_index" in parsed:
            raise ValueError("ambiguous file selection")
        if "file_index" in parsed:
            if raw_index is None or not raw_index.isascii() or not raw_index.isdigit() or int(raw_index) < 1:
                raise ValueError("invalid file index")
            index = int(raw_index) - 1
            file = None
        if "page" in parsed:
            if raw_page is None or not raw_page.isascii() or not raw_page.isdigit() or int(raw_page) < 1:
                raise ValueError("invalid page selection")
            selected_page = int(raw_page)
    if isinstance(file, int) and not isinstance(file, bool):
        index = file
    elif isinstance(file, str):
        matches = [position for position, item in enumerate(preview.files) if item.path == file]
        if matches:
            index = matches[0]
        elif file.isascii() and file.isdigit() and int(file) >= 1:
            index = int(file) - 1
        else:
            raise ValueError("unknown file selection")
    elif file is not None:
        raise ValueError("file selection must be an index or path")
    if page is not None:
        if not isinstance(page, int) or isinstance(page, bool):
            raise ValueError("page must be an integer")
        selected_page = page
    if not preview.files:
        index = 0
    elif not 0 <= index < len(preview.files):
        raise ValueError("file selection is outside its range")
    if selected_page < 1:
        raise ValueError("page selection is outside its range")
    return PreviewSelection(index, selected_page)


def _highlight_pair(old: str, new: str) -> tuple[str, str]:
    old_tokens = _TOKEN_RE.findall(old)
    new_tokens = _TOKEN_RE.findall(new)
    if (
        len(old) > MAX_INTRALINE_CHARS
        or len(new) > MAX_INTRALINE_CHARS
        or len(old_tokens) > MAX_INTRALINE_TOKENS
        or len(new_tokens) > MAX_INTRALINE_TOKENS
    ):
        return html.escape(old), html.escape(new)
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    old_parts: list[str] = []
    new_parts: list[str] = []
    for opcode, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        old_text = "".join(old_tokens[old_start:old_end])
        new_text = "".join(new_tokens[new_start:new_end])
        escaped_old = html.escape(old_text)
        escaped_new = html.escape(new_text)
        if opcode in {"replace", "delete"} and old_text and not old_text.isspace():
            escaped_old = f'<mark class="intra-delete">{escaped_old}</mark>'
        if opcode in {"replace", "insert"} and new_text and not new_text.isspace():
            escaped_new = f'<mark class="intra-add">{escaped_new}</mark>'
        old_parts.append(escaped_old)
        new_parts.append(escaped_new)
    return "".join(old_parts), "".join(new_parts)


def _code_row(
    kind: str,
    old_number: int | None,
    old_code: str,
    new_number: int | None,
    new_code: str,
    *,
    highlighted: tuple[str, str] | None = None,
) -> str:
    old_html, new_html = highlighted or (html.escape(old_code), html.escape(new_code))
    return (
        f'<tr class="{kind}"><td class="old-number">{old_number or ""}</td>'
        f'<td class="old-code"><code>{old_html}</code></td>'
        f'<td class="new-number">{new_number or ""}</td>'
        f'<td class="new-code"><code>{new_html}</code></td></tr>'
    )


def _meta_row(kind: str, text: str) -> str:
    return f'<tr class="{kind}"><td colspan="4"><code>{html.escape(text)}</code></td></tr>'


def _diff_rows(patch: str) -> list[str]:
    lines = patch.splitlines()
    rows: list[str] = []
    old_number: int | None = None
    new_number: int | None = None
    intraline_pairs = 0
    position = 0
    while position < len(lines):
        line = lines[position]
        hunk = _HUNK_RE.match(line)
        if hunk:
            old_number = int(hunk.group(1))
            new_number = int(hunk.group(3))
            rows.append(_meta_row("hunk", line))
            position += 1
            continue
        if old_number is None or new_number is None:
            rows.append(_meta_row("file-meta", line))
            position += 1
            continue
        if line.startswith("-"):
            deleted: list[str] = []
            while position < len(lines) and lines[position].startswith("-"):
                deleted.append(lines[position][1:])
                position += 1
            old_no_newline = ""
            if position < len(lines) and lines[position].startswith("\\ No newline at end of file"):
                old_no_newline = lines[position]
                position += 1
            added: list[str] = []
            while position < len(lines) and lines[position].startswith("+"):
                added.append(lines[position][1:])
                position += 1
            new_no_newline = ""
            if position < len(lines) and lines[position].startswith("\\ No newline at end of file"):
                new_no_newline = lines[position]
                position += 1
            for pair_index in range(max(len(deleted), len(added))):
                old_code = deleted[pair_index] if pair_index < len(deleted) else ""
                new_code = added[pair_index] if pair_index < len(added) else ""
                old_line = old_number if pair_index < len(deleted) else None
                new_line = new_number if pair_index < len(added) else None
                if old_line is not None:
                    old_number += 1
                if new_line is not None:
                    new_number += 1
                highlighted = None
                if old_line is not None and new_line is not None and intraline_pairs < MAX_INTRALINE_PAIRS:
                    highlighted = _highlight_pair(old_code, new_code)
                    intraline_pairs += 1
                rows.append(_code_row("changed", old_line, old_code, new_line, new_code, highlighted=highlighted))
            if old_no_newline:
                rows.append(_meta_row("no-newline old", old_no_newline))
            if new_no_newline:
                rows.append(_meta_row("no-newline new", new_no_newline))
            continue
        if line.startswith("+"):
            rows.append(_code_row("addition", None, "", new_number, line[1:]))
            new_number += 1
        elif line.startswith(" "):
            rows.append(_code_row("context", old_number, line[1:], new_number, line[1:]))
            old_number += 1
            new_number += 1
        elif line.startswith("\\ No newline at end of file"):
            rows.append(_meta_row("no-newline", line))
        else:
            rows.append(_meta_row("file-meta", line))
        position += 1
    return rows


def _omission_message(reason: str) -> str:
    return {
        OMITTED_BINARY: "Binary file content is not shown.",
        OMITTED_FILE_BYTES: "File content omitted: per-file 500 KiB limit exceeded.",
        OMITTED_FILE_LINES: "File content omitted: per-file 20,000 line limit exceeded.",
        OMITTED_TOTAL_BYTES: "File content omitted: aggregate 1 MiB limit exceeded.",
        OMITTED_TOTAL_LINES: "File content omitted: aggregate 20,000 line limit exceeded.",
    }[reason]


def _file_query(file_number: int, page: int = 1) -> str:
    return f"?file_index={file_number}&amp;page={page}"


def _scaffold(preview: DiffPreview, selected: PreviewFile, index: int, page: int, pages: int, rows: str) -> str:
    previous_file = ""
    next_file = ""
    if index > 0:
        previous_file = f'<a class="previous-file" href="{_file_query(index)}">Previous file</a>'
    if index + 1 < len(preview.files):
        next_file = f'<a class="next-file" href="{_file_query(index + 2)}">Next file</a>'
    previous_page = (
        f'<a class="previous-page" href="{_file_query(index + 1, page - 1)}">Previous page</a>' if page > 1 else ""
    )
    next_page = (
        f'<a class="next-page" href="{_file_query(index + 1, page + 1)}">Next page</a>' if page < pages else ""
    )
    old_path = f'<span class="old-path">from {html.escape(selected.old_path)}</span>' if selected.old_path else ""
    extra = (
        f'<p class="extra-files">{preview.extra_file_count} additional file(s) not listed (300-file limit).</p>'
        if preview.extra_file_count
        else ""
    )
    omission = (
        f'<p class="omission" data-reason="{selected.omission_reason}">{html.escape(_omission_message(selected.omission_reason))}</p>'
        if selected.omission_reason
        else ""
    )
    table = ""
    if selected.patch is not None:
        headings = (
            '<thead><tr><th scope="col">Old line</th><th scope="col">Old content</th>'
            '<th scope="col">New line</th><th scope="col">New content</th></tr></thead>'
        )
        table = f'<div class="diff-scroll"><table class="side-by-side-diff">{headings}<tbody>{rows}</tbody></table></div>'
    return (
        '<section class="trusted-diff-preview">'
        f'<header><nav class="file-nav">{previous_file}{next_file}</nav>'
        f'<p class="file-position">File {index + 1} of {len(preview.files)}</p>'
        f'<h3>{html.escape(selected.path)}</h3>{old_path}'
        f'<p class="file-summary"><span class="status">{selected.status}</span> '
        f'<span class="additions">+{selected.additions}</span> '
        f'<span class="deletions">−{selected.deletions}</span></p>{extra}{omission}</header>'
        f'{table}<nav class="page-nav">{previous_page}<span>Page {page} of {pages}</span>{next_page}</nav>'
        '</section>'
    )


def _partition_rows(rows: list[str], allowance: int) -> list[list[str]]:
    if not rows:
        return [[]]
    compact_omission = '<tr class="line-omitted"><td colspan="4">… line omitted to fit HTML budget</td></tr>'
    pages: list[list[str]] = []
    current: list[str] = []
    used = 0
    for row in rows:
        candidate = row
        size = len(candidate.encode("utf-8"))
        if size > allowance:
            candidate = compact_omission
            size = len(candidate.encode("utf-8"))
        if current and used + size > allowance:
            pages.append(current)
            current = []
            used = 0
        if size <= allowance:
            current.append(candidate)
            used += size
    if current or not pages:
        pages.append(current)
    return pages


def render_preview(
    preview: DiffPreview | bytes | str,
    selection: PreviewSelection | None = None,
    *,
    file: int | str | None = None,
    page: int | None = None,
    query: str | Mapping[str, Any] | None = None,
    html_budget: int = DEFAULT_HTML_BUDGET,
) -> str:
    """Render a trusted, escaped HTML fragment within ``html_budget`` bytes.

    Query values use ``file=<path-or-one-based-number>&page=<one-based-number>``;
    generated navigation uses the unambiguous one-based ``file_index`` key.
    Direct integer ``file`` arguments and ``PreviewSelection.file_index`` are
    zero-based for normal Python indexing.
    """

    if not isinstance(html_budget, int) or isinstance(html_budget, bool) or html_budget < 0:
        raise ValueError("HTML budget must be a non-negative integer")
    if isinstance(preview, (bytes, str)):
        loaded = load_preview(preview)
    elif isinstance(preview, DiffPreview):
        # Frozen dataclasses are the normal loaded representation, but callers
        # can still construct one directly. Revalidate before calling the HTML
        # trusted so malformed hand-built objects cannot bypass the boundary.
        loaded = load_preview(
            _canonical_json(
                {
                    "schema_version": preview.schema_version,
                    "metadata": dict(preview.metadata),
                    "files": [_file_dict(item) for item in preview.files],
                    "extra_file_count": preview.extra_file_count,
                }
            )
        )
    else:
        raise ValueError("preview must be a DiffPreview or artifact")
    if selection is not None and not isinstance(selection, PreviewSelection):
        raise ValueError("invalid preview selection")
    if not loaded.files:
        empty = '<section class="trusted-diff-preview empty">No changed files.</section>'
        return empty if len(empty.encode("utf-8")) <= html_budget else ""
    chosen = _selection(loaded, selection, file, page, query)
    selected = loaded.files[chosen.file_index]
    rows = _diff_rows(selected.patch) if selected.patch is not None else []

    # Reserve exact scaffold space, using worst-case page digits for stable packing.
    maximum_pages = max(1, len(rows))
    # Probe with both pagination links present and worst-case page digits.
    # A last-page probe would omit the next link and under-reserve space.
    probe = _scaffold(
        loaded, selected, chosen.file_index, maximum_pages, maximum_pages + 1, ""
    )
    allowance = html_budget - len(probe.encode("utf-8"))
    if allowance < 0:
        fallback = '<div class="trusted-diff-preview omitted">Diff preview omitted: HTML budget too small.</div>'
        return fallback if len(fallback.encode("utf-8")) <= html_budget else ""
    pages = _partition_rows(rows, allowance)
    if chosen.page > len(pages):
        raise ValueError("page selection is outside its range")
    selected_page = chosen.page
    rendered = _scaffold(
        loaded,
        selected,
        chosen.file_index,
        selected_page,
        len(pages),
        "".join(pages[selected_page - 1]),
    )
    if len(rendered.encode("utf-8")) > html_budget:
        # This can only happen if links with the selected page are longer than
        # the conservative probe. Fail closed rather than slicing trusted HTML.
        fallback = '<div class="trusted-diff-preview omitted">Diff preview omitted: HTML budget too small.</div>'
        return fallback if len(fallback.encode("utf-8")) <= html_budget else ""
    return rendered


__all__ = [
    "DEFAULT_HTML_BUDGET",
    "DiffPreview",
    "FilePatch",
    "MAX_ARTIFACT_BYTES",
    "MAX_EXTRA_FILE_COUNT",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "MAX_FILE_LINES",
    "MAX_TOTAL_BYTES",
    "MAX_TOTAL_LINES",
    "OMITTED_BINARY",
    "OMITTED_FILE_BYTES",
    "OMITTED_FILE_LINES",
    "OMITTED_TOTAL_BYTES",
    "OMITTED_TOTAL_LINES",
    "OmittedFilePatch",
    "PreviewFile",
    "PreviewSelection",
    "build_preview",
    "load_preview",
    "render_preview",
]
