"""Tests for the approval-request pipeline (summarisers + diff preview)."""

from __future__ import annotations

from pathlib import Path

import pytest

from alex.tools.fs import (
    _build_write_summariser,
    create_write_tool,
)
from alex.tools.permissions import (
    PERMISSION_WRITE,
    PermissionPolicy,
    PreviewBlock,
    ToolApprovalRequest,
    attach_approval_summariser,
    build_approval_request,
    gate_tool_with_policy,
    get_approval_summariser,
)
from alex.tools.shell import (
    _summarise_bash,
    _summarise_pwsh,
    create_bash_tool,
    create_pwsh_tool,
)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    return tmp_path


class TestSummariserAttachment:
    def test_write_has_summariser(self, sandbox: Path):
        tool = create_write_tool(allowed_roots=[sandbox])
        assert get_approval_summariser(tool) is not None

    def test_shell_tools_have_summarisers(self, sandbox: Path):
        bash = create_bash_tool(allowed_roots=[sandbox])
        pwsh = create_pwsh_tool(allowed_roots=[sandbox])
        assert get_approval_summariser(bash) is not None
        assert get_approval_summariser(pwsh) is not None


class TestBuildApprovalRequest:
    @pytest.mark.asyncio
    async def test_falls_back_to_default_digest(self, sandbox: Path):
        # An attached tool with no summariser should produce a key=value summary.
        tool = create_write_tool(allowed_roots=[sandbox])
        # Strip the summariser to test the fallback path explicitly.
        from alex.tools.permissions import _SUMMARISER_ATTR
        object.__setattr__(tool, _SUMMARISER_ATTR, None)
        request = await build_approval_request(tool, PERMISSION_WRITE, {"path": "x", "content": "y"})
        assert "path=x" in request.summary
        assert request.preview == []

    @pytest.mark.asyncio
    async def test_summariser_failure_degrades_gracefully(self, sandbox: Path):
        async def _bad(_args):
            raise RuntimeError("boom")

        tool = create_write_tool(allowed_roots=[sandbox])
        attach_approval_summariser(tool, _bad)
        request = await build_approval_request(tool, PERMISSION_WRITE, {"path": "x", "content": "y"})
        assert "summariser failed" in request.summary

    @pytest.mark.asyncio
    async def test_summariser_returning_str(self, sandbox: Path):
        async def _summary(_args):
            return "just a string"

        tool = create_write_tool(allowed_roots=[sandbox])
        attach_approval_summariser(tool, _summary)
        request = await build_approval_request(tool, PERMISSION_WRITE, {"path": "x", "content": "y"})
        assert request.summary == "just a string"
        assert request.preview == []


class TestFsWriteSummariser:
    @pytest.mark.asyncio
    async def test_creates_file_summary_when_missing(self, sandbox: Path):
        summariser = _build_write_summariser([sandbox])
        target = sandbox / "new.txt"
        summary, preview = await summariser({"path": str(target), "content": "hello"})
        assert "Create" in summary
        assert any("does not exist" in b.body for b in preview)

    @pytest.mark.asyncio
    async def test_diff_summary_for_existing_file(self, sandbox: Path):
        target = sandbox / "out.txt"
        target.write_bytes(b"line1\nline2\n")
        summariser = _build_write_summariser([sandbox])
        summary, preview = await summariser({
            "path": str(target),
            "content": "line1\nLINE2\nline3\n",
        })
        assert "Edit" in summary
        assert "+2" in summary
        assert "-1" in summary
        assert any(b.kind == "diff" for b in preview)
        diff_block = next(b for b in preview if b.kind == "diff")
        assert "+LINE2" in diff_block.body
        assert "+line3" in diff_block.body
        assert "-line2" in diff_block.body

    @pytest.mark.asyncio
    async def test_no_op_write_reports_no_changes(self, sandbox: Path):
        target = sandbox / "same.txt"
        target.write_bytes(b"same\n")
        summariser = _build_write_summariser([sandbox])
        summary, preview = await summariser({"path": str(target), "content": "same\n"})
        assert "No-op" in summary
        assert preview[0].body == "(no changes)"

    @pytest.mark.asyncio
    async def test_no_op_when_only_line_endings_differ(self, sandbox: Path):
        """CRLF on disk vs LF in payload should not register as a change."""
        target = sandbox / "crlf.txt"
        target.write_bytes(b"hello\r\nworld\r\n")
        summariser = _build_write_summariser([sandbox])
        summary, _ = await summariser({"path": str(target), "content": "hello\nworld\n"})
        assert "No-op" in summary

    @pytest.mark.asyncio
    async def test_path_outside_root_reports_block(self, tmp_path_factory):
        outside = tmp_path_factory.mktemp("elsewhere") / "x.txt"
        summariser = _build_write_summariser([tmp_path_factory.mktemp("sandbox")])
        summary, _ = await summariser({"path": str(outside), "content": "no"})
        assert "blocked" in summary

    @pytest.mark.asyncio
    async def test_binary_existing_file_suppresses_diff(self, sandbox: Path):
        target = sandbox / "blob.bin"
        target.write_bytes(b"\x00\x01\x02" * 1024)
        summariser = _build_write_summariser([sandbox])
        summary, preview = await summariser({
            "path": str(target), "content": "hello world",
        })
        assert "Overwrite" in summary
        assert any("binary" in b.body for b in preview)


class TestShellSummarisers:
    @pytest.mark.asyncio
    async def test_bash_renders_command_and_cwd(self):
        summary, preview = await _summarise_bash({
            "command": "echo hi | wc -l",
            "cwd": "/work",
            "timeout_seconds": 30,
        })
        assert summary.startswith("bash:")
        assert preview[0].kind == "code"
        assert "command:" in preview[0].body
        assert "echo hi | wc -l" in preview[0].body
        assert "/work" in preview[0].body

    @pytest.mark.asyncio
    async def test_pwsh_renders_command_and_cwd(self):
        summary, preview = await _summarise_pwsh({
            "command": "Get-ChildItem | Select-Object -First 1",
            "cwd": "C:\\work",
            "timeout_seconds": 30,
        })
        assert summary.startswith("pwsh:")
        assert preview[0].kind == "code"
        assert "Get-ChildItem" in preview[0].body
        assert "C:\\work" in preview[0].body


class TestEndToEndGating:
    @pytest.mark.asyncio
    async def test_gate_passes_request_to_hook(self, sandbox: Path):
        target = sandbox / "out.txt"
        target.write_text("old\n", encoding="utf-8")
        tool = create_write_tool(allowed_roots=[sandbox])

        captured: list[ToolApprovalRequest] = []

        async def _hook(req: ToolApprovalRequest):
            captured.append(req)
            return True

        policy = PermissionPolicy(confirm_hook=_hook)
        gate_tool_with_policy(tool, policy)
        result = await tool.ainvoke({"path": str(target), "content": "new\n"})

        assert result.startswith("Wrote")
        assert len(captured) == 1
        request = captured[0]
        assert request.tool_name == "write"
        assert request.permission == PERMISSION_WRITE
        assert "Edit" in request.summary
        assert any(b.kind == "diff" for b in request.preview)
        # The actual file content was updated only after approval.
        assert target.read_text(encoding="utf-8") == "new\n"

    @pytest.mark.asyncio
    async def test_denial_prevents_actual_write(self, sandbox: Path):
        target = sandbox / "out.txt"
        target.write_text("untouched\n", encoding="utf-8")
        tool = create_write_tool(allowed_roots=[sandbox])

        async def _hook(_req):
            return False

        policy = PermissionPolicy(confirm_hook=_hook)
        gate_tool_with_policy(tool, policy)
        result = await tool.ainvoke({"path": str(target), "content": "evil\n"})

        assert "blocked" in result
        # File untouched.
        assert target.read_text(encoding="utf-8") == "untouched\n"
