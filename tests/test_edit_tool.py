"""Tests for the Edit tool — precise string replacement with read-before-edit."""

from __future__ import annotations

from pathlib import Path

import pytest

from alex.tools.fs import (
    FileReadTracker,
    _build_edit_summariser,
    create_edit_tool,
    create_fs_read_tool,
    create_fs_write_tool,
)
from alex.tools.permissions import (
    PERMISSION_WRITE,
    PermissionPolicy,
    ToolApprovalRequest,
    gate_tool_with_policy,
    required_permission,
)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    return tmp_path


class TestEditMetadata:
    def test_declares_write_permission(self):
        tool = create_edit_tool()
        assert tool.name == "edit"
        assert required_permission(tool) == PERMISSION_WRITE


class TestReadBeforeEdit:
    @pytest.mark.asyncio
    async def test_refuses_when_file_never_read(self, sandbox: Path):
        target = sandbox / "code.py"
        target.write_text("print('hi')\n", encoding="utf-8")
        tracker = FileReadTracker()
        edit = create_edit_tool(allowed_roots=[sandbox], tracker=tracker)
        result = await edit.coroutine(
            file_path=str(target), old_string="hi", new_string="hello",
        )
        assert "must call fs_read" in result
        assert target.read_text(encoding="utf-8") == "print('hi')\n"

    @pytest.mark.asyncio
    async def test_passes_after_fs_read(self, sandbox: Path):
        target = sandbox / "code.py"
        target.write_text("print('hi')\n", encoding="utf-8")
        tracker = FileReadTracker()
        read = create_fs_read_tool(allowed_roots=[sandbox], tracker=tracker)
        edit = create_edit_tool(allowed_roots=[sandbox], tracker=tracker)

        await read.coroutine(path=str(target))
        result = await edit.coroutine(
            file_path=str(target), old_string="hi", new_string="hello",
        )
        assert result.startswith("Edited")
        assert target.read_text(encoding="utf-8") == "print('hello')\n"

    @pytest.mark.asyncio
    async def test_passes_after_fs_write(self, sandbox: Path):
        """fs_write also fingerprints the file so the agent can edit it next."""
        target = sandbox / "code.py"
        tracker = FileReadTracker()
        write = create_fs_write_tool(allowed_roots=[sandbox], tracker=tracker)
        edit = create_edit_tool(allowed_roots=[sandbox], tracker=tracker)

        await write.coroutine(path=str(target), content="print('hi')\n")
        result = await edit.coroutine(
            file_path=str(target), old_string="hi", new_string="hello",
        )
        assert result.startswith("Edited")
        assert target.read_text(encoding="utf-8") == "print('hello')\n"

    @pytest.mark.asyncio
    async def test_rejects_when_file_changed_externally(self, sandbox: Path):
        target = sandbox / "code.py"
        target.write_text("print('hi')\n", encoding="utf-8")
        tracker = FileReadTracker()
        read = create_fs_read_tool(allowed_roots=[sandbox], tracker=tracker)
        edit = create_edit_tool(allowed_roots=[sandbox], tracker=tracker)

        await read.coroutine(path=str(target))
        # External tweak — different size, different mtime.
        target.write_text("print('something else')\n", encoding="utf-8")

        result = await edit.coroutine(
            file_path=str(target), old_string="something", new_string="other",
        )
        assert "changed on disk" in result
        # Untouched.
        assert target.read_text(encoding="utf-8") == "print('something else')\n"


class TestEditSemantics:
    @pytest.mark.asyncio
    async def test_unique_match_replaces_once(self, sandbox: Path):
        target = sandbox / "f.py"
        target.write_text("alpha beta gamma\n", encoding="utf-8")
        tracker = FileReadTracker()
        await create_fs_read_tool(allowed_roots=[sandbox], tracker=tracker).coroutine(path=str(target))
        edit = create_edit_tool(allowed_roots=[sandbox], tracker=tracker)
        result = await edit.coroutine(
            file_path=str(target), old_string="beta", new_string="BETA",
        )
        assert result.startswith("Edited")
        assert target.read_text(encoding="utf-8") == "alpha BETA gamma\n"

    @pytest.mark.asyncio
    async def test_ambiguous_match_requires_replace_all(self, sandbox: Path):
        target = sandbox / "f.py"
        target.write_text("foo foo foo\n", encoding="utf-8")
        tracker = FileReadTracker()
        await create_fs_read_tool(allowed_roots=[sandbox], tracker=tracker).coroutine(path=str(target))
        edit = create_edit_tool(allowed_roots=[sandbox], tracker=tracker)

        result = await edit.coroutine(
            file_path=str(target), old_string="foo", new_string="bar",
        )
        assert "occurs 3 times" in result
        # File untouched.
        assert target.read_text(encoding="utf-8") == "foo foo foo\n"

    @pytest.mark.asyncio
    async def test_replace_all_changes_every_occurrence(self, sandbox: Path):
        target = sandbox / "f.py"
        target.write_text("foo foo foo\n", encoding="utf-8")
        tracker = FileReadTracker()
        await create_fs_read_tool(allowed_roots=[sandbox], tracker=tracker).coroutine(path=str(target))
        edit = create_edit_tool(allowed_roots=[sandbox], tracker=tracker)

        result = await edit.coroutine(
            file_path=str(target), old_string="foo", new_string="bar", replace_all=True,
        )
        assert "3 occurrences" in result
        assert target.read_text(encoding="utf-8") == "bar bar bar\n"

    @pytest.mark.asyncio
    async def test_missing_old_string_reported(self, sandbox: Path):
        target = sandbox / "f.py"
        target.write_text("hello\n", encoding="utf-8")
        tracker = FileReadTracker()
        await create_fs_read_tool(allowed_roots=[sandbox], tracker=tracker).coroutine(path=str(target))
        edit = create_edit_tool(allowed_roots=[sandbox], tracker=tracker)
        result = await edit.coroutine(
            file_path=str(target), old_string="missing", new_string="x",
        )
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_identical_old_and_new_rejected(self, sandbox: Path):
        target = sandbox / "f.py"
        target.write_text("x\n", encoding="utf-8")
        tracker = FileReadTracker()
        await create_fs_read_tool(allowed_roots=[sandbox], tracker=tracker).coroutine(path=str(target))
        edit = create_edit_tool(allowed_roots=[sandbox], tracker=tracker)
        result = await edit.coroutine(
            file_path=str(target), old_string="x", new_string="x",
        )
        assert "identical" in result.lower()

    @pytest.mark.asyncio
    async def test_empty_old_string_rejected(self, sandbox: Path):
        target = sandbox / "f.py"
        target.write_text("x\n", encoding="utf-8")
        tracker = FileReadTracker()
        await create_fs_read_tool(allowed_roots=[sandbox], tracker=tracker).coroutine(path=str(target))
        edit = create_edit_tool(allowed_roots=[sandbox], tracker=tracker)
        result = await edit.coroutine(
            file_path=str(target), old_string="", new_string="y",
        )
        assert "empty" in result.lower()

    @pytest.mark.asyncio
    async def test_missing_file_rejected(self, sandbox: Path):
        edit = create_edit_tool(allowed_roots=[sandbox], tracker=FileReadTracker())
        result = await edit.coroutine(
            file_path=str(sandbox / "nope.py"),
            old_string="x", new_string="y",
        )
        assert "does not exist" in result

    @pytest.mark.asyncio
    async def test_path_outside_root_rejected(self, sandbox: Path, tmp_path_factory):
        outside = tmp_path_factory.mktemp("other") / "x.txt"
        outside.write_text("hi\n", encoding="utf-8")
        edit = create_edit_tool(allowed_roots=[sandbox], tracker=FileReadTracker())
        result = await edit.coroutine(
            file_path=str(outside), old_string="hi", new_string="bye",
        )
        assert "outside" in result


class TestEditSummariser:
    @pytest.mark.asyncio
    async def test_summary_contains_unified_diff(self, sandbox: Path):
        target = sandbox / "f.py"
        target.write_bytes(b"alpha\nbeta\ngamma\n")
        summariser = _build_edit_summariser([sandbox])
        summary, preview = await summariser({
            "file_path": str(target),
            "old_string": "beta",
            "new_string": "BETA",
        })
        assert "Edit" in summary
        assert any(b.kind == "diff" for b in preview)
        diff_block = next(b for b in preview if b.kind == "diff")
        assert "+alpha" in diff_block.body or "alpha" in diff_block.body
        assert "+BETA" in diff_block.body
        assert "-beta" in diff_block.body

    @pytest.mark.asyncio
    async def test_summary_blocks_when_old_string_missing(self, sandbox: Path):
        target = sandbox / "f.py"
        target.write_bytes(b"hello\n")
        summariser = _build_edit_summariser([sandbox])
        summary, _ = await summariser({
            "file_path": str(target),
            "old_string": "missing",
            "new_string": "x",
        })
        assert "blocked" in summary
        assert "not found" in summary

    @pytest.mark.asyncio
    async def test_summary_warns_on_ambiguous_match(self, sandbox: Path):
        target = sandbox / "f.py"
        target.write_bytes(b"foo foo foo\n")
        summariser = _build_edit_summariser([sandbox])
        summary, _ = await summariser({
            "file_path": str(target),
            "old_string": "foo",
            "new_string": "bar",
        })
        assert "blocked" in summary
        assert "occurs 3 times" in summary


class TestEditEndToEndGate:
    @pytest.mark.asyncio
    async def test_denial_prevents_actual_edit(self, sandbox: Path):
        target = sandbox / "f.py"
        target.write_text("hello\n", encoding="utf-8")
        tracker = FileReadTracker()
        await create_fs_read_tool(allowed_roots=[sandbox], tracker=tracker).coroutine(path=str(target))

        tool = create_edit_tool(allowed_roots=[sandbox], tracker=tracker)

        async def _hook(_req: ToolApprovalRequest):
            return False

        gate_tool_with_policy(tool, PermissionPolicy(confirm_hook=_hook))
        result = await tool.ainvoke({
            "file_path": str(target),
            "old_string": "hello",
            "new_string": "evil",
        })
        assert "blocked" in result
        assert target.read_text(encoding="utf-8") == "hello\n"

    @pytest.mark.asyncio
    async def test_approval_passes_through_request(self, sandbox: Path):
        target = sandbox / "f.py"
        target.write_text("hello\n", encoding="utf-8")
        tracker = FileReadTracker()
        await create_fs_read_tool(allowed_roots=[sandbox], tracker=tracker).coroutine(path=str(target))

        tool = create_edit_tool(allowed_roots=[sandbox], tracker=tracker)
        captured: list[ToolApprovalRequest] = []

        async def _hook(req: ToolApprovalRequest):
            captured.append(req)
            return True

        gate_tool_with_policy(tool, PermissionPolicy(confirm_hook=_hook))
        result = await tool.ainvoke({
            "file_path": str(target),
            "old_string": "hello",
            "new_string": "world",
        })
        assert result.startswith("Edited")
        assert target.read_text(encoding="utf-8") == "world\n"
        assert len(captured) == 1
        assert captured[0].tool_name == "edit"
        assert any(b.kind == "diff" for b in captured[0].preview)
