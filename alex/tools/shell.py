"""Shell tools — two flavours: ``bash`` and ``pwsh``.

Earlier revisions exposed a single ``shell_run`` tool that took ``argv``
as a list and bypassed the shell entirely.  That made it impossible to
use pipes, redirection, or shell-only built-ins (``cd && ...``,
``Get-ChildItem | Select-Object``).  Most LLMs already speak both shell
dialects natively, so we expose them as separate tools and let the model
pick the right one for the host.

Both tools share:

- ``PERMISSION_SHELL`` permission (gated by the policy / TUI confirm)
- a wall-clock timeout (default 15 s, max 120 s)
- ``cwd`` constrained to *allowed_roots*
- stdout/stderr captured and truncated to 32 KiB each
- a per-shell hard deny list applied to *parsed* tokens (so ``rm`` is
  refused even when hidden inside ``echo hi && rm -rf /``)
- an approval summariser that shows the command, cwd, and timeout

Both tools are factory-built; if the underlying interpreter is missing
the host can choose to omit registration entirely.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import shutil
from pathlib import Path

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from alex.tools.permissions import (
    PERMISSION_SHELL,
    PreviewBlock,
    attach_approval_summariser,
)


TOOL_HINT_BASH = (
    "Use `bash` to run a Bash command on Unix-like hosts (or Git Bash / "
    "WSL on Windows). Supports pipes, redirection, &&/||, $VAR. "
    "Subject to the shell permission and a hard deny list (rm, dd, "
    "mkfs, sudo, …)."
)
TOOL_HINT_PWSH = (
    "Use `pwsh` to run a PowerShell command (PowerShell 7+ preferred, "
    "Windows PowerShell as fallback). Supports cmdlets, pipelines, "
    "splatting. Subject to the shell permission and a hard deny list "
    "(Remove-Item, Format-Volume, Stop-Computer, …)."
)


# ── safety bounds ─────────────────────────────────────────────────────

DEFAULT_TIMEOUT_SECONDS = 15
MAX_TIMEOUT_SECONDS = 120
MAX_OUTPUT_BYTES = 32 * 1024  # 32 KiB stdout + stderr each

# Shared destructive primitives — block these in *any* shell.
_BASE_DENIED_BINARIES = frozenset({
    "rm", "rmdir", "del", "shutdown", "reboot",
    "mkfs", "format", "dd",
    "chmod", "chown",
    "sudo", "su", "doas",
})

_BASH_DENIED_TOKENS = frozenset(_BASE_DENIED_BINARIES | {
    # POSIX-y synonyms / wrappers that commonly cause grief.
    "halt", "poweroff",
})

# PowerShell — block both the cmdlet form and the common aliases.
# Names compared case-insensitively in the parsed token check.
_PWSH_DENIED_TOKENS = frozenset({
    # destructive cmdlets
    "remove-item", "ri", "rd", "rmdir", "del", "erase",
    "remove-itemproperty",
    "format-volume", "clear-disk", "initialize-disk",
    "stop-computer", "restart-computer",
    "stop-service",
    "set-acl",
    "invoke-expression", "iex",
    # PowerShell sometimes calls into native cmd; block them too.
    "rm", "dd", "mkfs", "sudo",
})


def _resolve_cwd(raw: str | None, allowed_roots: list[Path]) -> Path:
    if not raw:
        return Path.cwd()
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
    raise ValueError(f"cwd '{raw}' is outside the allowed roots")


def _truncate(data: bytes) -> str:
    if len(data) <= MAX_OUTPUT_BYTES:
        return data.decode("utf-8", errors="replace")
    head = data[:MAX_OUTPUT_BYTES]
    return head.decode("utf-8", errors="replace") + "\n\n[Output truncated...]"


# ── bash ──────────────────────────────────────────────────────────────


class BashInput(BaseModel):
    command: str = Field(
        description=(
            "Bash command string. May use pipes, redirection, &&/||, "
            "subshells, and $VAR expansion."
        ),
    )
    cwd: str | None = Field(
        default=None,
        description="Working directory; must live under the allowed roots",
    )
    timeout_seconds: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        ge=1,
        le=MAX_TIMEOUT_SECONDS,
        description="Wall-clock timeout in seconds",
    )


def _bash_denylist_violation(command: str) -> str | None:
    """Return the offending token if *command* triggers the bash denylist."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        # Unbalanced quotes etc — let bash surface the parse error instead
        # of pretending we know what's in there.
        return None
    for token in tokens:
        # Strip a leading path component (``/usr/bin/rm`` → ``rm``).
        bare = Path(token).name.lower()
        bare_no_ext = bare.rsplit(".", 1)[0]
        if bare in _BASH_DENIED_TOKENS or bare_no_ext in _BASH_DENIED_TOKENS:
            return bare
    return None


def _resolve_bash() -> str | None:
    """Return the path to a usable Bash, preferring the system bash."""
    return shutil.which("bash")


def _make_bash(allowed_roots: list[Path]):
    async def _bash(
        command: str,
        cwd: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> str:
        if not isinstance(command, str):
            return "Error: command must be a string"
        if not command or not command.strip():
            return "Error: command must not be empty"

        violation = _bash_denylist_violation(command)
        if violation is not None:
            return f"Error: '{violation}' is on the hard deny list"

        bash_path = _resolve_bash()
        if bash_path is None:
            return "Error: 'bash' not found in PATH (install Git Bash / WSL or use the pwsh tool)"

        try:
            workdir = _resolve_cwd(cwd, allowed_roots)
        except ValueError as e:
            return f"Error: {e}"

        try:
            proc = await asyncio.create_subprocess_exec(
                bash_path, "-lc", command,
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return f"Error spawning bash: {type(e).__name__}: {e}"

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return f"Error: bash command timed out after {timeout_seconds}s"

        return (
            f"shell: bash\n"
            f"command: {command}\n"
            f"cwd: {workdir}\n"
            f"exit_code: {proc.returncode}\n"
            f"--- stdout ---\n{_truncate(stdout)}\n"
            f"--- stderr ---\n{_truncate(stderr)}"
        )

    return _bash


async def _summarise_bash(args: dict) -> tuple[str, list[PreviewBlock]]:
    command = str(args.get("command") or "")
    cwd = args.get("cwd") or "(working directory)"
    timeout = args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    summary = f"bash: {command[:160]}"
    body = (
        f"command:\n  {command}\n"
        f"cwd: {cwd}\n"
        f"timeout: {timeout}s"
    )
    return summary, [PreviewBlock(title="bash command", body=body, kind="code")]


def create_bash_tool(*, allowed_roots: list[Path] | None = None) -> StructuredTool:
    roots = allowed_roots or [Path.cwd()]
    tool = StructuredTool.from_function(
        coroutine=_make_bash(roots),
        name="bash",
        description=(
            "Run a Bash command (supports pipes, redirection, &&/||). "
            "Output is captured and truncated. Subject to the shell "
            "permission and a hard deny list of destructive primitives."
        ),
        args_schema=BashInput,
        metadata={"required_permission": PERMISSION_SHELL},
    )
    attach_approval_summariser(tool, _summarise_bash)
    return tool


# ── pwsh ──────────────────────────────────────────────────────────────


class PwshInput(BaseModel):
    command: str = Field(
        description=(
            "PowerShell command string. Cmdlets, pipelines, and "
            "splatting are all supported."
        ),
    )
    cwd: str | None = Field(
        default=None,
        description="Working directory; must live under the allowed roots",
    )
    timeout_seconds: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        ge=1,
        le=MAX_TIMEOUT_SECONDS,
        description="Wall-clock timeout in seconds",
    )


# PowerShell tokenisation is much looser than POSIX; rather than
# parsing the language we look for cmdlet-shaped or short-name tokens
# anywhere in the command and compare case-insensitively.
_PWSH_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def _pwsh_denylist_violation(command: str) -> str | None:
    for match in _PWSH_TOKEN_RE.finditer(command):
        token = match.group(0).lower()
        if token in _PWSH_DENIED_TOKENS:
            return token
    return None


def _resolve_pwsh() -> str | None:
    """Prefer PowerShell 7+ (``pwsh``) but fall back to Windows PowerShell."""
    for candidate in ("pwsh", "powershell"):
        path = shutil.which(candidate)
        if path is not None:
            return path
    return None


def _make_pwsh(allowed_roots: list[Path]):
    async def _pwsh(
        command: str,
        cwd: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> str:
        if not isinstance(command, str):
            return "Error: command must be a string"
        if not command or not command.strip():
            return "Error: command must not be empty"

        violation = _pwsh_denylist_violation(command)
        if violation is not None:
            return f"Error: '{violation}' is on the hard deny list"

        pwsh_path = _resolve_pwsh()
        if pwsh_path is None:
            return (
                "Error: neither 'pwsh' nor 'powershell' found in PATH "
                "(install PowerShell 7+ or use the bash tool)"
            )

        try:
            workdir = _resolve_cwd(cwd, allowed_roots)
        except ValueError as e:
            return f"Error: {e}"

        # ``-NoProfile`` keeps startup fast and deterministic.
        # ``-NonInteractive`` is critical so the prompt can never block.
        argv = [
            pwsh_path,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return f"Error spawning pwsh: {type(e).__name__}: {e}"

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return f"Error: pwsh command timed out after {timeout_seconds}s"

        engine = Path(pwsh_path).name
        return (
            f"shell: {engine}\n"
            f"command: {command}\n"
            f"cwd: {workdir}\n"
            f"exit_code: {proc.returncode}\n"
            f"--- stdout ---\n{_truncate(stdout)}\n"
            f"--- stderr ---\n{_truncate(stderr)}"
        )

    return _pwsh


async def _summarise_pwsh(args: dict) -> tuple[str, list[PreviewBlock]]:
    command = str(args.get("command") or "")
    cwd = args.get("cwd") or "(working directory)"
    timeout = args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    summary = f"pwsh: {command[:160]}"
    body = (
        f"command:\n  {command}\n"
        f"cwd: {cwd}\n"
        f"timeout: {timeout}s"
    )
    return summary, [PreviewBlock(title="pwsh command", body=body, kind="code")]


def create_pwsh_tool(*, allowed_roots: list[Path] | None = None) -> StructuredTool:
    roots = allowed_roots or [Path.cwd()]
    tool = StructuredTool.from_function(
        coroutine=_make_pwsh(roots),
        name="pwsh",
        description=(
            "Run a PowerShell command (PowerShell 7+ preferred, Windows "
            "PowerShell as fallback). Output is captured and truncated. "
            "Subject to the shell permission and a hard deny list of "
            "destructive cmdlets."
        ),
        args_schema=PwshInput,
        metadata={"required_permission": PERMISSION_SHELL},
    )
    attach_approval_summariser(tool, _summarise_pwsh)
    return tool


# ── host helpers ──────────────────────────────────────────────────────


def detect_available_shells() -> dict[str, str]:
    """Return a map of ``{tool_name: resolved_path}`` for shells on this host.

    Hosts use this to decide which shell tool(s) to register.  When
    nothing is available the agent simply has no shell access — which
    is also a reasonable default for sandboxed deployments.
    """
    found: dict[str, str] = {}
    bash = _resolve_bash()
    if bash is not None:
        found["bash"] = bash
    pwsh = _resolve_pwsh()
    if pwsh is not None:
        found["pwsh"] = pwsh
    return found


def create_available_shell_tools(
    *,
    allowed_roots: list[Path] | None = None,
) -> list[StructuredTool]:
    """Build whichever of ``bash`` / ``pwsh`` the host actually supports.

    Hosts can call this from ``main.py`` to register both shells when
    available (e.g. WSL on Windows ships both) or fall back to a single
    one without bothering with platform branches themselves.
    """
    found = detect_available_shells()
    tools: list[StructuredTool] = []
    if "bash" in found:
        tools.append(create_bash_tool(allowed_roots=allowed_roots))
    if "pwsh" in found:
        tools.append(create_pwsh_tool(allowed_roots=allowed_roots))
    return tools
