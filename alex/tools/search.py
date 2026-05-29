"""Search tools — ``grep`` (content search) and ``glob`` (path search).

Both tools resolve paths against *allowed_roots* and refuse to escape
the working tree.  ``grep`` prefers the system ``rg`` (ripgrep) binary
when available — it is faster, honours ``.gitignore`` automatically,
and supports rich options like ``-t TYPE`` and multiline matching —
and falls back to a pure-Python regex walker otherwise.

Both tools register a lightweight summariser so the agent's audit log
records what pattern was used and where; neither requires an explicit
permission grant beyond the default ``read`` allow-list.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from alex.tools._binary import looks_like_binary
from alex.tools._path import resolve_path_in_allowed_roots
from alex.tools.permissions import (
    PERMISSION_READ,
    PreviewBlock,
    attach_approval_summariser,
)


TOOL_HINT_GREP = (
    "Use `grep` to search file contents by regex. Pass `output_mode='content'` "
    "to see matching lines (with line numbers and optional context); "
    "`'files_with_matches'` (default) to just list matching files; "
    "`'count'` to summarise per-file hit counts. Honours .gitignore when "
    "ripgrep is installed."
)
TOOL_HINT_GLOB = (
    "Use `glob` to find files by name (e.g. `**/*.py`). Returns matching "
    "paths sorted by modification time (newest first). Use `grep` to "
    "search by content."
)


# ── safety bounds ─────────────────────────────────────────────────────

DEFAULT_HEAD_LIMIT = 250
DEFAULT_GREP_TIMEOUT_SECONDS = 30
DEFAULT_GLOB_HEAD_LIMIT = 200
PURE_PY_FILE_BYTE_LIMIT = 2 * 1024 * 1024  # 2 MiB per file in fallback walker
BINARY_PROBE_BYTES = 4096

# Directories the pure-Python walker silently skips (ripgrep handles its own
# ignores via .gitignore).
_DEFAULT_PRUNE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "node_modules", ".pnpm-store",
    ".venv", "venv", ".tox",
    "dist", "build", "target",
    ".idea", ".vscode",
})

# Map ``type`` aliases (used when ripgrep is unavailable) to extensions.
_TYPE_TO_EXTS: dict[str, tuple[str, ...]] = {
    "py": (".py",),
    "js": (".js", ".mjs", ".cjs"),
    "ts": (".ts",),
    "tsx": (".tsx",),
    "jsx": (".jsx",),
    "go": (".go",),
    "rs": (".rs",),
    "java": (".java",),
    "kt": (".kt", ".kts"),
    "rb": (".rb",),
    "php": (".php",),
    "c": (".c", ".h"),
    "cpp": (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"),
    "cs": (".cs",),
    "swift": (".swift",),
    "md": (".md", ".markdown"),
    "json": (".json",),
    "yaml": (".yaml", ".yml"),
    "toml": (".toml",),
    "html": (".html", ".htm"),
    "css": (".css", ".scss", ".sass"),
    "shell": (".sh", ".bash", ".zsh", ".fish"),
    "sql": (".sql",),
    "txt": (".txt",),
}


def _resolve_safe_path(raw: str | None, allowed_roots: list[Path]) -> Path:
    """Resolve *raw* against *allowed_roots*, defaulting to the first root."""
    return resolve_path_in_allowed_roots(
        raw, allowed_roots, default_to_first_root=True, label="path",
    )


def _looks_binary(probe: bytes) -> bool:
    return looks_like_binary(probe)


# ── grep ──────────────────────────────────────────────────────────────


class GrepInput(BaseModel):
    """Input schema for the ``grep`` tool.

    Field names use Python identifiers; the JSON schema exposed to the
    LLM uses ripgrep-style aliases (``-i``, ``-n``, ``-A`` …) so the
    model can call the tool with the same flags it knows from rg.
    """

    model_config = ConfigDict(populate_by_name=True)

    pattern: str = Field(description="Regex pattern (ripgrep / Python re syntax)")
    path: str | None = Field(
        default=None,
        description="File or directory to search (default: current working directory)",
    )
    glob: str | None = Field(
        default=None,
        description="Optional filename glob to filter candidate files, e.g. '*.py'",
    )
    output_mode: str = Field(
        default="files_with_matches",
        description="One of: files_with_matches | content | count",
    )
    ignore_case: bool = Field(
        default=False, alias="-i",
        description="Case-insensitive matching",
    )
    line_number: bool | None = Field(
        default=None, alias="-n",
        description="Show line numbers (defaults to true under output_mode='content')",
    )
    after_context: int = Field(
        default=0, ge=0, le=20, alias="-A",
        description="Lines of trailing context for content mode",
    )
    before_context: int = Field(
        default=0, ge=0, le=20, alias="-B",
        description="Lines of leading context for content mode",
    )
    context: int = Field(
        default=0, ge=0, le=20, alias="-C",
        description="Symmetric context (overrides -A/-B when set)",
    )
    head_limit: int = Field(
        default=DEFAULT_HEAD_LIMIT, ge=1, le=10000,
        description="Max number of result lines/files (default 250)",
    )
    multiline: bool = Field(
        default=False,
        description="Allow patterns to span newlines (uses re.DOTALL / rg --multiline)",
    )
    type: str | None = Field(
        default=None,
        description="Filter by file type alias (e.g. 'py', 'ts', 'md')",
    )


@dataclass
class _GrepOptions:
    pattern: str
    path: Path
    glob: str | None
    output_mode: str
    ignore_case: bool
    show_line_numbers: bool
    after_context: int
    before_context: int
    head_limit: int
    multiline: bool
    type: str | None


def _normalise_grep_args(
    *,
    pattern: str,
    path: Path,
    glob: str | None,
    output_mode: str,
    ignore_case: bool,
    line_number: bool | None,
    context: int,
    after_context: int,
    before_context: int,
    head_limit: int,
    multiline: bool,
    type: str | None,
) -> _GrepOptions:
    output_mode = (output_mode or "files_with_matches").strip().lower()
    if output_mode not in ("files_with_matches", "content", "count"):
        raise ValueError(
            f"output_mode must be 'files_with_matches', 'content', or 'count'; got {output_mode!r}"
        )
    show_ln = line_number if line_number is not None else (output_mode == "content")
    if context > 0:
        before_context = max(before_context, context)
        after_context = max(after_context, context)
    return _GrepOptions(
        pattern=pattern,
        path=path,
        glob=glob,
        output_mode=output_mode,
        ignore_case=ignore_case,
        show_line_numbers=show_ln,
        after_context=after_context,
        before_context=before_context,
        head_limit=head_limit,
        multiline=multiline,
        type=type,
    )


def _ripgrep_available() -> bool:
    return shutil.which("rg") is not None


def _build_rg_argv(opts: _GrepOptions) -> list[str]:
    argv = ["rg", "--color", "never"]
    if opts.ignore_case:
        argv.append("-i")
    if opts.glob:
        argv.extend(["-g", opts.glob])
    if opts.type:
        argv.extend(["-t", opts.type])
    if opts.multiline:
        argv.extend(["-U", "--multiline-dotall"])

    if opts.output_mode == "files_with_matches":
        argv.append("--files-with-matches")
    elif opts.output_mode == "count":
        argv.append("--count")
    else:  # content
        argv.append("--no-heading")
        if opts.show_line_numbers:
            argv.append("-n")
        else:
            argv.append("-N")
        if opts.before_context:
            argv.extend(["-B", str(opts.before_context)])
        if opts.after_context:
            argv.extend(["-A", str(opts.after_context)])

    argv.extend(["--", opts.pattern, str(opts.path)])
    return argv


async def _run_rg(opts: _GrepOptions) -> str:
    argv = _build_rg_argv(opts)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        return f"Error spawning ripgrep: {type(e).__name__}: {e}"

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=DEFAULT_GREP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return "Error: ripgrep timed out"

    if proc.returncode == 1 and not stdout:
        return _format_no_matches(opts)
    if proc.returncode not in (0, 1):
        return f"Error: ripgrep exited {proc.returncode}: {stderr.decode('utf-8', 'replace').strip()}"

    text = stdout.decode("utf-8", errors="replace")
    return _truncate_grep_output(text, opts)


def _format_no_matches(opts: _GrepOptions) -> str:
    return f"No matches for /{opts.pattern}/ in {opts.path}"


def _render_grep_result(
    *,
    opts: _GrepOptions,
    body_lines: list[str],
    matched_files: list[str],
    body_suffix: str = "",
) -> str:
    header = f"Pattern: /{opts.pattern}/  mode: {opts.output_mode}  scope: {opts.path}\n"
    file_lines = matched_files[: opts.head_limit]
    files_suffix = (
        f"\n... ({len(matched_files) - opts.head_limit} more files truncated, raise head_limit to see them)"
        if len(matched_files) > opts.head_limit else ""
    )
    files_block = "Files:\n" + "\n".join(file_lines) + files_suffix
    body = "\n".join(body_lines)
    return header + files_block + "\n\n" + body + body_suffix


def _truncate_grep_output(text: str, opts: _GrepOptions) -> str:
    lines = text.splitlines()
    if not lines:
        return _format_no_matches(opts)
    truncated = lines[: opts.head_limit]
    suffix = (
        f"\n... ({len(lines) - opts.head_limit} more lines truncated, raise head_limit to see them)"
        if len(lines) > opts.head_limit else ""
    )
    header = f"Pattern: /{opts.pattern}/  mode: {opts.output_mode}  scope: {opts.path}\n"
    return header + "\n".join(truncated) + suffix


async def _run_rg_files_with_matches(opts: _GrepOptions) -> list[str] | None:
    file_opts = _GrepOptions(
        pattern=opts.pattern,
        path=opts.path,
        glob=opts.glob,
        output_mode="files_with_matches",
        ignore_case=opts.ignore_case,
        show_line_numbers=False,
        after_context=0,
        before_context=0,
        head_limit=opts.head_limit,
        multiline=opts.multiline,
        type=opts.type,
    )
    argv = _build_rg_argv(file_opts)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return None

    try:
        stdout, _stderr = await asyncio.wait_for(
            proc.communicate(), timeout=DEFAULT_GREP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return None

    if proc.returncode == 1 and not stdout:
        return []
    if proc.returncode not in (0, 1):
        return None
    return [line for line in stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]


async def _run_grep_with_matches(opts: _GrepOptions) -> tuple[str, list[str] | None]:
    result = await _run_rg(opts)
    if result.startswith("Error:") or result.startswith("Error spawning") or result.startswith("No matches for "):
        return result, []
    return result, await _run_rg_files_with_matches(opts)


# Pure-Python fallback ─────────────────────────────────────────────────


def _iter_candidate_files(opts: _GrepOptions) -> list[Path]:
    base = opts.path
    glob = opts.glob
    type_exts = _TYPE_TO_EXTS.get(opts.type or "", ())

    if base.is_file():
        files = [base]
    else:
        files = []
        for root, dirs, names in os.walk(base):
            # In-place prune of common heavy directories.
            dirs[:] = [d for d in dirs if d not in _DEFAULT_PRUNE_DIRS and not d.startswith(".")]
            for name in names:
                p = Path(root) / name
                files.append(p)
    out: list[Path] = []
    for p in files:
        if glob is not None:
            try:
                if not p.match(glob):
                    continue
            except Exception:
                continue
        if type_exts and p.suffix not in type_exts:
            continue
        out.append(p)
    return out


def _read_text_safely(path: Path) -> str | None:
    try:
        if path.stat().st_size > PURE_PY_FILE_BYTE_LIMIT:
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if _looks_binary(data[:BINARY_PROBE_BYTES]):
        return None
    return data.decode("utf-8", errors="replace")


def _compile_regex(opts: _GrepOptions) -> re.Pattern[str] | str:
    flags = 0
    if opts.ignore_case:
        flags |= re.IGNORECASE
    if opts.multiline:
        flags |= re.DOTALL
    try:
        return re.compile(opts.pattern, flags)
    except re.error as e:
        return f"Error: invalid regex {opts.pattern!r}: {e}"


def _python_grep(opts: _GrepOptions) -> str:
    compiled = _compile_regex(opts)
    if isinstance(compiled, str):
        return compiled

    candidates = _iter_candidate_files(opts)
    output_lines: list[str] = []
    matched_files: list[str] = []
    truncated_overflow = False  # True when we stopped collecting because of head_limit

    if opts.output_mode == "files_with_matches":
        for path in candidates:
            text = _read_text_safely(path)
            if text is None:
                continue
            if compiled.search(text):
                if len(output_lines) >= opts.head_limit:
                    truncated_overflow = True
                    break
                path_str = str(path)
                matched_files.append(path_str)
                output_lines.append(path_str)
    elif opts.output_mode == "count":
        for path in candidates:
            text = _read_text_safely(path)
            if text is None:
                continue
            count = len(compiled.findall(text))
            if count > 0:
                if len(output_lines) >= opts.head_limit:
                    truncated_overflow = True
                    break
                matched_files.append(str(path))
                output_lines.append(f"{path}:{count}")
    else:  # content
        for path in candidates:
            if len(output_lines) >= opts.head_limit:
                truncated_overflow = True
                break
            text = _read_text_safely(path)
            if text is None:
                continue
            file_lines = text.splitlines()
            if opts.multiline:
                # Multiline mode reports the line containing the start of the match.
                for m in compiled.finditer(text):
                    if len(output_lines) >= opts.head_limit:
                        truncated_overflow = True
                        break
                    if str(path) not in matched_files:
                        matched_files.append(str(path))
                    start_line = text.count("\n", 0, m.start()) + 1
                    snippet = file_lines[start_line - 1] if start_line - 1 < len(file_lines) else ""
                    output_lines.append(_format_content_line(path, start_line, snippet, opts))
            else:
                produced = _python_content_match(path, file_lines, compiled, opts)
                if produced:
                    matched_files.append(str(path))
                room = opts.head_limit - len(output_lines)
                if len(produced) > room:
                    output_lines.extend(produced[:room])
                    truncated_overflow = True
                    break
                output_lines.extend(produced)

    if not output_lines:
        return _format_no_matches(opts)
    suffix = "\n... (results truncated; raise head_limit to see more)" if truncated_overflow else ""
    return _render_grep_result(
        opts=opts,
        body_lines=output_lines,
        matched_files=matched_files,
        body_suffix=suffix,
    )


def _python_content_match(
    path: Path,
    file_lines: list[str],
    compiled: re.Pattern[str],
    opts: _GrepOptions,
) -> list[str]:
    out: list[str] = []
    last_emitted = -1
    leading: deque[tuple[int, str]] = deque(maxlen=opts.before_context)
    pending_after = 0

    for idx, line in enumerate(file_lines, start=1):
        if compiled.search(line):
            # Flush leading context (avoid duplicating already-emitted lines).
            for ctx_idx, ctx_line in list(leading):
                if ctx_idx <= last_emitted:
                    continue
                out.append(_format_content_line(path, ctx_idx, ctx_line, opts, marker="-"))
                last_emitted = ctx_idx
            out.append(_format_content_line(path, idx, line, opts))
            last_emitted = idx
            pending_after = opts.after_context
        elif pending_after > 0:
            out.append(_format_content_line(path, idx, line, opts, marker="-"))
            last_emitted = idx
            pending_after -= 1
        leading.append((idx, line))

    return out


def _format_content_line(
    path: Path,
    line_no: int,
    text: str,
    opts: _GrepOptions,
    marker: str = ":",
) -> str:
    if opts.show_line_numbers:
        return f"{path}{marker}{line_no}{marker}{text}"
    return f"{path}{marker}{text}"


def _make_grep(allowed_roots: list[Path]):
    async def _grep(
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        output_mode: str = "files_with_matches",
        ignore_case: bool = False,
        line_number: bool | None = None,
        after_context: int = 0,
        before_context: int = 0,
        context: int = 0,
        head_limit: int = DEFAULT_HEAD_LIMIT,
        multiline: bool = False,
        type: str | None = None,
    ) -> str:
        try:
            target = _resolve_safe_path(path, allowed_roots)
        except ValueError as e:
            return f"Error: {e}"
        if not target.exists():
            return f"Error: '{path}' does not exist"

        try:
            opts = _normalise_grep_args(
                pattern=pattern,
                path=target,
                glob=glob,
                output_mode=output_mode,
                ignore_case=ignore_case,
                line_number=line_number,
                context=context,
                after_context=after_context,
                before_context=before_context,
                head_limit=head_limit,
                multiline=multiline,
                type=type,
            )
        except ValueError as e:
            return f"Error: {e}"

        if _ripgrep_available():
            result, matched_files = await _run_grep_with_matches(opts)
            if matched_files is None or result.startswith("Error:") or result.startswith("Error spawning") or result.startswith("No matches for "):
                return result
            body_lines = result.splitlines()
            if not body_lines:
                return result
            if body_lines[0].startswith("Pattern: /"):
                body_lines = body_lines[1:]
            return _render_grep_result(
                opts=opts,
                body_lines=body_lines,
                matched_files=matched_files,
            )
        return await asyncio.to_thread(_python_grep, opts)

    return _grep


async def _summarise_grep(args: dict) -> tuple[str, list[PreviewBlock]]:
    pattern = str(args.get("pattern") or "")
    path = str(args.get("path") or "(cwd)")
    mode = str(args.get("output_mode") or "files_with_matches")
    summary = f"grep /{pattern}/ in {path} ({mode})"
    body_lines = [f"pattern: {pattern}", f"path:    {path}", f"mode:    {mode}"]
    for label, key in (
        ("glob", "glob"),
        ("type", "type"),
        ("ignore_case", "ignore_case"),
        ("multiline", "multiline"),
        ("context", "context"),
        ("before", "before_context"),
        ("after", "after_context"),
        ("head_limit", "head_limit"),
    ):
        value = args.get(key) or args.get(f"-{label[0]}") if label in {"i", "n"} else args.get(key)
        if value not in (None, "", False, 0):
            body_lines.append(f"{label}: {value}")
    return summary, [PreviewBlock(title="grep options", body="\n".join(body_lines), kind="code")]


def create_grep_tool(*, allowed_roots: list[Path] | None = None) -> StructuredTool:
    roots = allowed_roots or [Path.cwd()]
    tool = StructuredTool.from_function(
        coroutine=_make_grep(roots),
        name="grep",
        description=(
            "Search file contents by regex (ripgrep-compatible). "
            "Returns matching files (default), matching lines (output_mode='content'), "
            "or per-file match counts (output_mode='count'). Honours .gitignore "
            "when ripgrep is installed."
        ),
        args_schema=GrepInput,
        metadata={"required_permission": PERMISSION_READ},
    )
    attach_approval_summariser(tool, _summarise_grep)
    return tool


# ── glob ──────────────────────────────────────────────────────────────


class GlobInput(BaseModel):
    pattern: str = Field(description="Glob pattern, e.g. '**/*.py' or 'src/**/*.tsx'")
    path: str | None = Field(
        default=None,
        description="Search root (default: current working directory)",
    )


def _make_glob(allowed_roots: list[Path]):
    async def _glob(pattern: str, path: str | None = None) -> str:
        try:
            base = _resolve_safe_path(path, allowed_roots)
        except ValueError as e:
            return f"Error: {e}"
        if not base.exists():
            return f"Error: '{path}' does not exist"
        if not base.is_dir():
            return f"Error: '{path}' is not a directory"
        if not pattern:
            return "Error: pattern is required"

        def _walk() -> tuple[list[Path], int]:
            try:
                raw = list(base.glob(pattern))
            except Exception as e:
                raise RuntimeError(f"glob failed: {type(e).__name__}: {e}") from e
            files = [p for p in raw if p.is_file()]

            def _mtime(p: Path) -> float:
                try:
                    return p.stat().st_mtime
                except OSError:
                    return 0.0

            files.sort(key=_mtime, reverse=True)
            return files, len(files)

        try:
            files, total = await asyncio.to_thread(_walk)
        except RuntimeError as e:
            return f"Error: {e}"

        if not files:
            return f"No files match {pattern!r} under {base}"

        head = files[:DEFAULT_GLOB_HEAD_LIMIT]
        body = "\n".join(str(p) for p in head)
        suffix = (
            f"\n... ({total - DEFAULT_GLOB_HEAD_LIMIT} more truncated)"
            if total > DEFAULT_GLOB_HEAD_LIMIT else ""
        )
        return f"Found {total} files (sorted by mtime, newest first)\n\n{body}{suffix}"

    return _glob


async def _summarise_glob(args: dict) -> tuple[str, list[PreviewBlock]]:
    pattern = str(args.get("pattern") or "")
    path = str(args.get("path") or "(cwd)")
    summary = f"glob {pattern} in {path}"
    body = f"pattern: {pattern}\npath:    {path}"
    return summary, [PreviewBlock(title="glob", body=body, kind="code")]


def create_glob_tool(*, allowed_roots: list[Path] | None = None) -> StructuredTool:
    roots = allowed_roots or [Path.cwd()]
    tool = StructuredTool.from_function(
        coroutine=_make_glob(roots),
        name="glob",
        description=(
            "Find files by name pattern (e.g. '**/*.py'). Returns matching "
            "paths sorted by modification time (newest first). For content "
            "search use the `grep` tool."
        ),
        args_schema=GlobInput,
        metadata={"required_permission": PERMISSION_READ},
    )
    attach_approval_summariser(tool, _summarise_glob)
    return tool
