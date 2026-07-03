"""Tests for the bash and pwsh shell tools.

The two tools share infrastructure but exercise different interpreters
and denylists.  Tests skip individually when the underlying interpreter
is missing on the host so the suite stays green on Linux-only and
Windows-only runners alike.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import alex.tools.shell as shell_mod
from alex.tools.permissions import PERMISSION_SHELL, required_permission
from alex.tools.shell import (
    _bash_denylist_violation,
    _format_shell_result,
    _pwsh_denylist_violation,
    _resolve_bash,
    _resolve_pwsh,
    _truncate,
    create_available_shell_tools,
    create_bash_tool,
    create_pwsh_tool,
    detect_available_shells,
)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    return tmp_path


_BASH_AVAILABLE = _resolve_bash() is not None
_PWSH_AVAILABLE = _resolve_pwsh() is not None


# ── shared metadata ───────────────────────────────────────────────────


class TestMetadata:
    def test_bash_declares_shell_permission(self):
        tool = create_bash_tool()
        assert tool.name == "bash"
        assert required_permission(tool) == PERMISSION_SHELL

    def test_pwsh_declares_shell_permission(self):
        tool = create_pwsh_tool()
        assert tool.name == "pwsh"
        assert required_permission(tool) == PERMISSION_SHELL


class TestShellFormattingHelpers:
    def test_format_shell_result_returns_empty_for_success_with_no_output(self):
        result = _format_shell_result(stdout=b"", stderr=b"", exit_code=0)
        assert result == ""

    def test_format_shell_result_prefers_stderr_for_failures(self):
        result = _format_shell_result(stdout=b"partial", stderr=b"boom", exit_code=2)
        assert result == "Error: command exited with code 2\nboom"

    def test_format_shell_result_includes_stdout_and_stderr_on_success(self):
        result = _format_shell_result(stdout=b"hello\n", stderr=b"warning\n", exit_code=0)
        assert result == "hello\nwarning"

    def test_truncate_drops_partial_utf8_tail_before_decoding(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(shell_mod, "MAX_OUTPUT_BYTES", 4)
        result = _truncate("你好".encode("utf-8"))
        assert result == "你\n\n[Output truncated...]"
        assert "\ufffd" not in result


# ── bash ──────────────────────────────────────────────────────────────


@pytest.mark.skipif(not _BASH_AVAILABLE, reason="bash not installed on this host")
class TestBashTool:
    @pytest.mark.asyncio
    async def test_runs_simple_command(self, sandbox: Path):
        tool = create_bash_tool(allowed_roots=[sandbox])
        result = await tool.coroutine(command="echo hello")
        assert result.strip() == "hello"

    @pytest.mark.asyncio
    async def test_supports_pipes(self, sandbox: Path):
        tool = create_bash_tool(allowed_roots=[sandbox])
        result = await tool.coroutine(command="printf 'a\\nb\\nc\\n' | wc -l")
        # wc -l counts lines; we should see "3" somewhere in stdout.
        assert result.strip() == "3"

    @pytest.mark.asyncio
    async def test_cwd_outside_roots_blocked(self, sandbox: Path, tmp_path_factory):
        outside = tmp_path_factory.mktemp("elsewhere")
        tool = create_bash_tool(allowed_roots=[sandbox])
        result = await tool.coroutine(command="echo ok", cwd=str(outside))
        assert result.startswith("Error:")
        assert "outside" in result

    @pytest.mark.asyncio
    async def test_timeout(self, sandbox: Path):
        tool = create_bash_tool(allowed_roots=[sandbox])
        result = await tool.coroutine(command="sleep 5", timeout_seconds=1)
        assert "timed out" in result


class TestBashDenylist:
    """The deny-list parser runs without the interpreter being present."""

    def test_blocks_rm(self):
        assert _bash_denylist_violation("rm -rf /") == "rm"

    def test_blocks_rm_after_pipe(self):
        # Tokenisation must catch hidden destructive primitives.
        assert _bash_denylist_violation("echo hi && rm -rf /tmp/junk") == "rm"

    def test_blocks_full_path(self):
        assert _bash_denylist_violation("/bin/rm -f /tmp/x") == "rm"

    def test_allows_safe_command(self):
        assert _bash_denylist_violation("ls -la") is None
        assert _bash_denylist_violation("echo 'rm is mentioned in a string'") is None

    def test_blocks_sudo(self):
        assert _bash_denylist_violation("sudo apt update") == "sudo"

    @pytest.mark.skipif(not _BASH_AVAILABLE, reason="bash not installed")
    @pytest.mark.asyncio
    async def test_denylist_short_circuits_subprocess(self, sandbox: Path):
        tool = create_bash_tool(allowed_roots=[sandbox])
        result = await tool.coroutine(command="rm -rf /tmp/anything")
        assert result.startswith("Error:")
        assert "deny" in result


class TestBashEmptyAndMissing:
    @pytest.mark.asyncio
    async def test_rejects_empty_command(self, sandbox: Path):
        tool = create_bash_tool(allowed_roots=[sandbox])
        result = await tool.coroutine(command="   ")
        assert result.startswith("Error:")

    @pytest.mark.asyncio
    async def test_rejects_non_string_command(self, sandbox: Path):
        tool = create_bash_tool(allowed_roots=[sandbox])
        result = await tool.coroutine(command=["ls", "-la"])  # type: ignore[arg-type]
        assert result.startswith("Error:")


# ── pwsh ──────────────────────────────────────────────────────────────


@pytest.mark.skipif(not _PWSH_AVAILABLE, reason="pwsh/powershell not installed on this host")
class TestPwshTool:
    @pytest.mark.asyncio
    async def test_runs_simple_command(self, sandbox: Path):
        tool = create_pwsh_tool(allowed_roots=[sandbox])
        result = await tool.coroutine(command="Write-Output 'hello'")
        assert result.strip() == "hello"

    @pytest.mark.asyncio
    async def test_supports_pipeline(self, sandbox: Path):
        tool = create_pwsh_tool(allowed_roots=[sandbox])
        result = await tool.coroutine(
            command="1..3 | Measure-Object | Select-Object -ExpandProperty Count",
        )
        assert result.strip() == "3"

    @pytest.mark.asyncio
    async def test_cwd_outside_roots_blocked(self, sandbox: Path, tmp_path_factory):
        outside = tmp_path_factory.mktemp("elsewhere")
        tool = create_pwsh_tool(allowed_roots=[sandbox])
        result = await tool.coroutine(command="Write-Output ok", cwd=str(outside))
        assert result.startswith("Error:")
        assert "outside" in result

    @pytest.mark.asyncio
    async def test_timeout(self, sandbox: Path):
        tool = create_pwsh_tool(allowed_roots=[sandbox])
        result = await tool.coroutine(command="Start-Sleep -Seconds 5", timeout_seconds=1)
        assert "timed out" in result


class TestPwshDenylist:
    def test_blocks_remove_item(self):
        assert _pwsh_denylist_violation("Remove-Item C:\\Windows -Recurse -Force") == "remove-item"

    def test_blocks_alias_ri(self):
        # 'ri' is the PowerShell alias for Remove-Item.
        assert _pwsh_denylist_violation("ri foo.txt") == "ri"

    def test_blocks_format_volume(self):
        assert _pwsh_denylist_violation("Get-Volume | Format-Volume") == "format-volume"

    def test_blocks_stop_computer(self):
        assert _pwsh_denylist_violation("Stop-Computer -Force") == "stop-computer"

    def test_blocks_iex(self):
        assert _pwsh_denylist_violation("$x = 'evil'; Invoke-Expression $x") == "invoke-expression"
        assert _pwsh_denylist_violation("$x = 'evil'; iex $x") == "iex"

    def test_allows_safe_pipeline(self):
        assert _pwsh_denylist_violation(
            "Get-ChildItem | Select-Object -First 3"
        ) is None

    def test_case_insensitive(self):
        assert _pwsh_denylist_violation("REMOVE-ITEM x") == "remove-item"
        assert _pwsh_denylist_violation("STOP-COMPUTER") == "stop-computer"

    @pytest.mark.skipif(not _PWSH_AVAILABLE, reason="pwsh not installed")
    @pytest.mark.asyncio
    async def test_denylist_short_circuits_subprocess(self, sandbox: Path):
        tool = create_pwsh_tool(allowed_roots=[sandbox])
        result = await tool.coroutine(command="Remove-Item C:\\Windows -Recurse")
        assert result.startswith("Error:")
        assert "deny" in result


class TestPwshEmptyAndMissing:
    @pytest.mark.asyncio
    async def test_rejects_empty_command(self, sandbox: Path):
        tool = create_pwsh_tool(allowed_roots=[sandbox])
        result = await tool.coroutine(command="")
        assert result.startswith("Error:")


# ── host detection ────────────────────────────────────────────────────




