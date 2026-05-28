"""Git inspection tool — read-only ``git status`` / ``diff`` / ``log``.

This tool wraps a small handful of safe ``git`` invocations.  Mutating
operations (``commit``, ``push``, ``reset``) are deliberately omitted;
the agent can shell out via ``shell_run`` after the user has granted
the ``shell`` permission for those.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from alex.tools.permissions import PERMISSION_READ


TOOL_HINT = (
    "Use `git_inspect` to read-only inspect the working tree: "
    "action='status' shows changes, 'diff' shows pending edits, "
    "'log' shows recent commits."
)


MAX_OUTPUT_BYTES = 32 * 1024
DEFAULT_TIMEOUT_SECONDS = 10

GitAction = Literal["status", "diff", "log"]


class GitInspectInput(BaseModel):
    action: GitAction = Field(description="Which inspection to run")
    path: str | None = Field(default=None, description="Optional path to scope the command to")
    max_count: int = Field(default=20, ge=1, le=200, description="Max log entries (action='log')")


def _resolve_repo(raw: str | None, allowed_roots: list[Path]) -> Path:
    candidate = Path(raw).expanduser() if raw else Path.cwd()
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


def _truncate(data: bytes) -> str:
    if len(data) <= MAX_OUTPUT_BYTES:
        return data.decode("utf-8", errors="replace")
    return data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace") + "\n\n[Output truncated...]"


def _build_argv(action: str, max_count: int, scope: Path | None) -> list[str]:
    if action == "status":
        return ["git", "status", "--short", "--branch"]
    if action == "diff":
        argv = ["git", "diff", "--no-color"]
        if scope is not None:
            argv.extend(["--", str(scope)])
        return argv
    if action == "log":
        return [
            "git", "log",
            f"--max-count={max_count}",
            "--no-color",
            "--pretty=format:%h %ad %an %s",
            "--date=short",
        ]
    raise ValueError(f"unsupported action: {action}")


def _make_git_inspect(allowed_roots: list[Path]):
    async def _git_inspect(action: GitAction, path: str | None = None, max_count: int = 20) -> str:
        try:
            workdir = _resolve_repo(path, allowed_roots)
        except ValueError as e:
            return f"Error: {e}"

        if shutil.which("git") is None:
            return "Error: 'git' executable not found in PATH"

        try:
            argv = _build_argv(action, max_count, workdir if action == "diff" and path else None)
        except ValueError as e:
            return f"Error: {e}"

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workdir if workdir.is_dir() else workdir.parent),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return f"Error spawning git: {type(e).__name__}: {e}"

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=DEFAULT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return f"Error: git {action} timed out"

        body = _truncate(stdout) if stdout else ""
        err = _truncate(stderr) if stderr else ""
        if proc.returncode != 0 and not body:
            return f"Error: git {action} failed ({proc.returncode}): {err.strip()}"

        return (
            f"action: {action}\n"
            f"cwd: {workdir}\n"
            f"exit_code: {proc.returncode}\n"
            f"--- stdout ---\n{body}\n"
            f"--- stderr ---\n{err}"
        )

    return _git_inspect


def create_git_inspect_tool(*, allowed_roots: list[Path] | None = None) -> StructuredTool:
    roots = allowed_roots or [Path.cwd()]
    return StructuredTool.from_function(
        coroutine=_make_git_inspect(roots),
        name="git_inspect",
        description=(
            "Inspect a git repository read-only. "
            "action='status' lists changes, 'diff' shows pending edits, "
            "'log' shows recent commits. Mutating operations are not supported."
        ),
        args_schema=GitInspectInput,
        metadata={"required_permission": PERMISSION_READ},
    )
