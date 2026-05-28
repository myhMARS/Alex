"""Filesystem tools — read, write, and edit files with safety bounds.

Three tools live here:

- ``read``     — bounded text read with binary detection
- ``write``    — atomic full-file write
- ``edit``      — precise string replacement (read-before-edit enforced)

All three share a :class:`FileReadTracker` so the agent must observe a
file (via ``read``) or have just written it (via ``write``)
before ``edit`` will accept a change to that path.  The tracker also
detects external modifications: if the file's ``mtime``/``size``
changed since the last read, the agent must re-read before editing.

Each destructive tool registers an approval summariser so the TUI
confirm modal renders a unified diff against the current contents
before any write happens.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from alex.tools.permissions import (
    PERMISSION_READ,
    PERMISSION_WRITE,
    PreviewBlock,
    attach_approval_summariser,
)


TOOL_HINT_READ = (
    "Use `read` to inspect a small text file under the working tree. "
    "Returns up to `max_bytes` of content; binary files are refused."
)
TOOL_HINT_WRITE = (
    "Use `write` to create a new file or rewrite an existing one in full. "
    "For small in-place edits prefer `edit` — it shows a tighter diff and "
    "preserves untouched content. The user must approve a diff before any "
    "actual write happens."
)
TOOL_HINT_EDIT = (
    "Use `edit` to make a precise string replacement in an existing file. "
    "Required: file_path, old_string, new_string. Optional: replace_all "
    "(default false). The old_string must occur exactly once unless "
    "replace_all=true; you must call `read` (or have just written the "
    "file via `write`) before `edit` will be accepted."
)


# ── safety bounds ─────────────────────────────────────────────────────

DEFAULT_MAX_READ_BYTES = 256 * 1024           # 256 KiB
DEFAULT_MAX_WRITE_BYTES = 1 * 1024 * 1024     # 1 MiB
DEFAULT_MAX_EDIT_PAYLOAD = 1 * 1024 * 1024    # cap final post-edit size
BINARY_PROBE_BYTES = 4096
DIFF_MAX_LINES = 200
PREVIEW_HEAD_BYTES = 4 * 1024


# ── read tracker ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _FileFingerprint:
    """Cheap identity for a file based on its filesystem metadata."""

    mtime_ns: int
    size: int
    sha256: str


class FileReadTracker:
    """Records which files the agent has observed during this session.

    Used by ``edit`` to enforce the *read-before-edit* invariant.  A
    file counts as "observed" when:

    - ``read`` returned its content successfully, or
    - ``write`` just wrote a new revision (the agent therefore
      knows the resulting state).

    The tracker stores a fingerprint (``mtime_ns``, ``size``, ``sha256``)
    so a file edited externally between read and edit is detected.
    """

    def __init__(self) -> None:
        self._records: dict[Path, _FileFingerprint] = {}

    def record(self, path: Path, content: bytes) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        digest = hashlib.sha256(content).hexdigest()
        self._records[path.resolve(strict=False)] = _FileFingerprint(
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            sha256=digest,
        )

    def forget(self, path: Path) -> None:
        self._records.pop(path.resolve(strict=False), None)

    def is_fresh(self, path: Path) -> tuple[bool, str]:
        """Return ``(ok, reason)``: has the agent observed *path* recently?

        ``reason`` is empty on success and otherwise carries an
        actionable message ("call read first" / "file changed").
        """
        key = path.resolve(strict=False)
        record = self._records.get(key)
        if record is None:
            return False, "you must call read on this file before editing"
        try:
            stat = path.stat()
        except FileNotFoundError:
            return False, "file no longer exists; call read again"
        except OSError as e:
            return False, f"cannot stat file: {type(e).__name__}: {e}"
        if stat.st_mtime_ns != record.mtime_ns or stat.st_size != record.size:
            return False, "file changed on disk since last read; call read again"
        return True, ""


# ── path / encoding helpers ───────────────────────────────────────────


class ReadInput(BaseModel):
    path: str = Field(description="Path to the file relative to the current working directory")
    max_bytes: int = Field(
        default=DEFAULT_MAX_READ_BYTES,
        ge=1,
        le=DEFAULT_MAX_READ_BYTES * 4,
        description="Maximum number of bytes to read",
    )
    encoding: str = Field(default="utf-8", description="Text encoding to decode with")


class WriteInput(BaseModel):
    path: str = Field(description="Path to the file relative to the current working directory")
    content: str = Field(description="Full text content to write")
    encoding: str = Field(default="utf-8", description="Text encoding for the payload")
    create_dirs: bool = Field(default=False, description="Create parent directories if missing")


class EditInput(BaseModel):
    file_path: str = Field(description="Path to the file (absolute or relative to the working directory)")
    old_string: str = Field(description="Exact text to replace; must match verbatim")
    new_string: str = Field(description="Replacement text")
    replace_all: bool = Field(
        default=False,
        description="Replace every occurrence (default: only one — old_string must be unique)",
    )
    encoding: str = Field(default="utf-8", description="Text encoding used to read and write the file")


def _resolve_safe_path(raw: str, allowed_roots: list[Path]) -> Path:
    if not raw:
        raise ValueError("path is required")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = candidate.resolve(strict=False)

    for root in allowed_roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return resolved
        except ValueError:
            continue
    raise ValueError(f"path '{raw}' is outside the allowed roots")


def _looks_binary(probe: bytes) -> bool:
    """Heuristic: null bytes are a strong binary signal; otherwise try
    to decode as UTF-8.  A successful decode means the content is text    (handles CJK, emoji, and other multi-byte sequences).  If the probe
    ends mid-sequence we retry without the tail bytes.  Only when UTF-8
    decode genuinely fails do we fall back to the printable-ASCII ratio."""
    if b"\x00" in probe:
        return True
    if not probe:
        return False
    try:
        probe.decode("utf-8")
        return False
    except UnicodeDecodeError as e:
        # A decode error at the very end of the probe is likely a
        # truncated multi-byte character — retry without the tail.
        if e.end >= len(probe) - 3:
            try:
                probe[:e.start].decode("utf-8")
                return False
            except UnicodeDecodeError:
                pass
    text_chars = bytes(range(0x20, 0x7F)) + b"\n\r\t\b\f"
    nontext = sum(1 for b in probe if b not in text_chars)
    return nontext / len(probe) > 0.3


def _normalise_for_diff(text: str) -> str:
    """Normalise line endings so CRLF/LF differences don't pollute the diff.

    The actual write preserves whatever the caller sent — this is only
    used to build the user-facing preview.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _truncate_diff(lines: list[str], max_lines: int = DIFF_MAX_LINES) -> str:
    if len(lines) <= max_lines:
        return "".join(lines)
    head = "".join(lines[:max_lines])
    return f"{head}\n... ({len(lines) - max_lines} more lines truncated)"


def _atomic_write(target: Path, payload: bytes) -> None:
    """Write *payload* to *target* atomically using a sibling tempfile."""
    fd, tmp_path = tempfile.mkstemp(prefix=".alex.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_existing_text(path: Path, encoding: str) -> tuple[str, str]:
    """Return ``(text, note)`` describing the current file contents.

    ``note`` is empty on success; otherwise it explains why the diff
    could not be computed (binary file, IO error, etc.).
    """
    if not path.exists():
        return "", "(file does not exist — will be created)"
    if not path.is_file():
        return "", f"(path is not a regular file: {path})"
    try:
        with open(path, "rb") as f:
            probe = f.read(BINARY_PROBE_BYTES)
            if _looks_binary(probe):
                return "", "(existing file looks binary — diff suppressed)"
            rest = f.read(max(0, DEFAULT_MAX_READ_BYTES - len(probe)))
        return (probe + rest).decode(encoding, errors="replace"), ""
    except OSError as e:
        return "", f"(could not read existing file: {type(e).__name__}: {e})"


# ── read ─────────────────────────────────────────────────────────────


def _make_read(allowed_roots: list[Path], tracker: FileReadTracker | None):
    async def _read(path: str, max_bytes: int = DEFAULT_MAX_READ_BYTES, encoding: str = "utf-8") -> str:
        try:
            target = _resolve_safe_path(path, allowed_roots)
        except ValueError as e:
            return f"Error: {e}"
        if not target.exists():
            return f"Error: '{path}' does not exist"
        if not target.is_file():
            return f"Error: '{path}' is not a regular file"

        try:
            with open(target, "rb") as f:
                full_bytes = f.read()
            probe = full_bytes[:BINARY_PROBE_BYTES]
            if _looks_binary(probe):
                return f"Error: '{path}' looks like a binary file; refusing to read"

            visible = full_bytes[:max_bytes]
            payload = visible.decode(encoding, errors="replace")
            truncated = len(full_bytes) > max_bytes
            suffix = "\n\n[Content truncated...]" if truncated else ""

            if tracker is not None:
                tracker.record(target, full_bytes)

            return f"Path: {target}\nBytes: {min(len(full_bytes), max_bytes)}\n\n{payload}{suffix}"
        except OSError as e:
            return f"Error reading {path}: {type(e).__name__}: {e}"

    return _read


# ── write ────────────────────────────────────────────────────────────


def _make_write(
    allowed_roots: list[Path],
    max_write_bytes: int,
    tracker: FileReadTracker | None,
):
    async def _write(
        path: str,
        content: str,
        encoding: str = "utf-8",
        create_dirs: bool = False,
    ) -> str:
        try:
            target = _resolve_safe_path(path, allowed_roots)
        except ValueError as e:
            return f"Error: {e}"

        payload = content.encode(encoding, errors="replace")
        if len(payload) > max_write_bytes:
            return f"Error: payload {len(payload)} bytes exceeds limit {max_write_bytes}"

        if create_dirs:
            target.parent.mkdir(parents=True, exist_ok=True)
        elif not target.parent.exists():
            return f"Error: parent directory '{target.parent}' does not exist"

        try:
            _atomic_write(target, payload)
        except OSError as e:
            return f"Error writing {path}: {type(e).__name__}: {e}"

        if tracker is not None:
            tracker.record(target, payload)
        return f"Wrote {len(payload)} bytes to {target}"

    return _write


# ── edit ──────────────────────────────────────────────────────────────


def _make_edit(
    allowed_roots: list[Path],
    max_payload_bytes: int,
    tracker: FileReadTracker | None,
):
    async def _edit(
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        encoding: str = "utf-8",
    ) -> str:
        try:
            target = _resolve_safe_path(file_path, allowed_roots)
        except ValueError as e:
            return f"Error: {e}"

        if not target.exists():
            return f"Error: '{file_path}' does not exist; use write to create new files"
        if not target.is_file():
            return f"Error: '{file_path}' is not a regular file"

        if old_string == new_string:
            return "Error: old_string and new_string are identical (no-op)"
        if not old_string:
            return "Error: old_string must not be empty"

        if tracker is not None:
            ok, reason = tracker.is_fresh(target)
            if not ok:
                return f"Error: {reason}"

        try:
            with open(target, "rb") as f:
                raw = f.read()
        except OSError as e:
            return f"Error reading {file_path}: {type(e).__name__}: {e}"

        if _looks_binary(raw[:BINARY_PROBE_BYTES]):
            return f"Error: '{file_path}' looks binary; refusing to edit"

        text = raw.decode(encoding, errors="replace")
        occurrences = text.count(old_string)
        if occurrences == 0:
            return "Error: old_string not found in file"
        if occurrences > 1 and not replace_all:
            return (
                f"Error: old_string occurs {occurrences} times — pass "
                f"replace_all=true or extend old_string to make it unique"
            )

        if replace_all:
            updated = text.replace(old_string, new_string)
            replaced = occurrences
        else:
            updated = text.replace(old_string, new_string, 1)
            replaced = 1

        payload = updated.encode(encoding, errors="replace")
        if len(payload) > max_payload_bytes:
            return f"Error: resulting payload {len(payload)} bytes exceeds limit {max_payload_bytes}"

        try:
            _atomic_write(target, payload)
        except OSError as e:
            return f"Error writing {file_path}: {type(e).__name__}: {e}"

        if tracker is not None:
            tracker.record(target, payload)
        suffix = f" ({replaced} occurrences)" if replaced > 1 else ""
        return f"Edited {target}{suffix}"

    return _edit


# ── approval summarisers ──────────────────────────────────────────────


def _build_write_summariser(allowed_roots: list[Path]):
    async def _summarise(args: dict) -> tuple[str, list[PreviewBlock]]:
        raw_path = str(args.get("path") or "")
        encoding = str(args.get("encoding") or "utf-8")
        new_content = str(args.get("content") or "")

        try:
            target = _resolve_safe_path(raw_path, allowed_roots)
        except ValueError as e:
            return (f"write blocked: {e}", [])

        existing_text, note = _read_existing_text(target, encoding)
        new_bytes = len(new_content.encode(encoding, errors="replace"))

        if note:
            preview_blocks: list[PreviewBlock] = [
                PreviewBlock(title=f"Path: {target}", body=note),
            ]
            if not target.exists():
                head = new_content
                if len(head.encode(encoding, errors="replace")) > PREVIEW_HEAD_BYTES:
                    head = head.encode(encoding, errors="replace")[:PREVIEW_HEAD_BYTES].decode(
                        encoding, errors="replace",
                    ) + "\n... (preview truncated)"
                preview_blocks.append(PreviewBlock(
                    title="New content (preview)", body=head, kind="code",
                ))
            summary = (
                f"Create {target} ({new_bytes} bytes)"
                if not target.exists()
                else f"Overwrite {target} ({new_bytes} bytes)"
            )
            return summary, preview_blocks

        diff = list(difflib.unified_diff(
            _normalise_for_diff(existing_text).splitlines(keepends=True),
            _normalise_for_diff(new_content).splitlines(keepends=True),
            fromfile=f"a/{target.name}",
            tofile=f"b/{target.name}",
            n=3,
        ))
        if not diff:
            summary = f"No-op write: {target} (content already matches)"
            return summary, [PreviewBlock(title=str(target), body="(no changes)")]

        added = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
        summary = f"Edit {target} (+{added} / -{removed}, {new_bytes} bytes total)"
        body = _truncate_diff(diff)
        return summary, [PreviewBlock(title="Proposed change", body=body, kind="diff")]

    return _summarise


def _build_edit_summariser(allowed_roots: list[Path]):
    async def _summarise(args: dict) -> tuple[str, list[PreviewBlock]]:
        raw_path = str(args.get("file_path") or "")
        encoding = str(args.get("encoding") or "utf-8")
        old_string = str(args.get("old_string") or "")
        new_string = str(args.get("new_string") or "")
        replace_all = bool(args.get("replace_all", False))

        try:
            target = _resolve_safe_path(raw_path, allowed_roots)
        except ValueError as e:
            return (f"edit blocked: {e}", [])

        if not target.exists():
            return (
                f"edit blocked: '{raw_path}' does not exist (use write to create)",
                [],
            )
        if old_string == new_string:
            return ("edit blocked: old_string and new_string are identical", [])

        existing_text, note = _read_existing_text(target, encoding)
        if note:
            return (f"Edit {target}", [PreviewBlock(title=str(target), body=note)])

        occurrences = existing_text.count(old_string)
        if occurrences == 0:
            return (
                f"edit blocked: old_string not found in {target}",
                [PreviewBlock(
                    title="old_string",
                    body=_clip(old_string, 400),
                    kind="code",
                )],
            )
        if occurrences > 1 and not replace_all:
            return (
                f"edit blocked: old_string occurs {occurrences} times "
                f"(pass replace_all=true or extend the snippet)",
                [PreviewBlock(
                    title="old_string",
                    body=_clip(old_string, 400),
                    kind="code",
                )],
            )

        if replace_all:
            updated = existing_text.replace(old_string, new_string)
        else:
            updated = existing_text.replace(old_string, new_string, 1)

        diff = list(difflib.unified_diff(
            _normalise_for_diff(existing_text).splitlines(keepends=True),
            _normalise_for_diff(updated).splitlines(keepends=True),
            fromfile=f"a/{target.name}",
            tofile=f"b/{target.name}",
            n=3,
        ))
        added = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
        scope = f"all {occurrences} occurrences" if replace_all else "1 occurrence"
        summary = f"Edit {target} (+{added} / -{removed}, {scope})"
        body = _truncate_diff(diff) if diff else "(no visible diff)"
        return summary, [PreviewBlock(title="Proposed change", body=body, kind="diff")]

    return _summarise


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated)"


# ── factories ─────────────────────────────────────────────────────────


def create_read_tool(
    *,
    allowed_roots: list[Path] | None = None,
    tracker: FileReadTracker | None = None,
) -> StructuredTool:
    roots = allowed_roots or [Path.cwd()]
    return StructuredTool.from_function(
        coroutine=_make_read(roots, tracker),
        name="read",
        description=(
            "Read a text file inside the working tree. Returns the raw "
            "content (truncated to max_bytes). Refuses binary files and "
            "paths outside the working directory."
        ),
        args_schema=ReadInput,
        metadata={"required_permission": PERMISSION_READ},
    )


def create_write_tool(
    *,
    allowed_roots: list[Path] | None = None,
    max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES,
    tracker: FileReadTracker | None = None,
) -> StructuredTool:
    roots = allowed_roots or [Path.cwd()]
    tool = StructuredTool.from_function(
        coroutine=_make_write(roots, max_write_bytes, tracker),
        name="write",
        description=(
            "Create a new file or rewrite an existing one in full. Writes "
            "are atomic (temp file + replace). Refuses paths outside the "
            "working directory. The user sees a diff and must confirm "
            "before the write actually happens. For small in-place "
            "changes prefer the `edit` tool."
        ),
        args_schema=WriteInput,
        metadata={"required_permission": PERMISSION_WRITE},
    )
    attach_approval_summariser(tool, _build_write_summariser(roots))
    return tool


def create_edit_tool(
    *,
    allowed_roots: list[Path] | None = None,
    max_payload_bytes: int = DEFAULT_MAX_EDIT_PAYLOAD,
    tracker: FileReadTracker | None = None,
) -> StructuredTool:
    """Create the precise-string-replacement ``edit`` tool.

    *tracker* enforces the read-before-edit invariant.  Pass
    ``tracker=None`` to disable the check (useful for tests or
    headless scripts where the agent owns the file lifecycle).
    """
    roots = allowed_roots or [Path.cwd()]
    tool = StructuredTool.from_function(
        coroutine=_make_edit(roots, max_payload_bytes, tracker),
        name="edit",
        description=(
            "Replace an exact string in an existing file. The old_string "
            "must occur once unless replace_all=true. Requires the file "
            "to have been read (via read) or just written (via "
            "write); if it was modified externally since then, the "
            "agent must call read again first. The user sees a diff "
            "and must confirm before the change is applied."
        ),
        args_schema=EditInput,
        metadata={"required_permission": PERMISSION_WRITE},
    )
    attach_approval_summariser(tool, _build_edit_summariser(roots))
    return tool
