"""Tests for fs_read / fs_write tools."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from alex.tools.fs import (
    DEFAULT_MAX_READ_BYTES,
    create_fs_read_tool,
    create_fs_write_tool,
)
from alex.tools.permissions import (
    PERMISSION_READ,
    PERMISSION_WRITE,
    required_permission,
)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    return tmp_path


class TestFsReadTool:
    def test_metadata_declares_read_permission(self):
        tool = create_fs_read_tool()
        assert tool.name == "fs_read"
        assert required_permission(tool) == PERMISSION_READ

    @pytest.mark.asyncio
    async def test_reads_text_file(self, sandbox: Path):
        target = sandbox / "hello.txt"
        target.write_text("hello world", encoding="utf-8")
        tool = create_fs_read_tool(allowed_roots=[sandbox])
        result = await tool.ainvoke({"path": str(target)})
        assert "hello world" in result

    @pytest.mark.asyncio
    async def test_refuses_path_outside_root(self, sandbox: Path, tmp_path_factory):
        outside = tmp_path_factory.mktemp("other")
        evil = outside / "secret.txt"
        evil.write_text("nope", encoding="utf-8")
        tool = create_fs_read_tool(allowed_roots=[sandbox])
        result = await tool.ainvoke({"path": str(evil)})
        assert result.startswith("Error:")
        assert "outside" in result

    @pytest.mark.asyncio
    async def test_refuses_binary_file(self, sandbox: Path):
        target = sandbox / "blob.bin"
        target.write_bytes(b"\x00\x01\x02\x03" * 256)
        tool = create_fs_read_tool(allowed_roots=[sandbox])
        result = await tool.ainvoke({"path": str(target)})
        assert result.startswith("Error:")
        assert "binary" in result

    @pytest.mark.asyncio
    async def test_truncates_large_files(self, sandbox: Path):
        target = sandbox / "big.txt"
        target.write_text("a" * (DEFAULT_MAX_READ_BYTES + 100), encoding="utf-8")
        tool = create_fs_read_tool(allowed_roots=[sandbox])
        result = await tool.ainvoke({"path": str(target), "max_bytes": 1024})
        assert "[Content truncated...]" in result


class TestFsWriteTool:
    def test_metadata_declares_write_permission(self):
        tool = create_fs_write_tool()
        assert tool.name == "fs_write"
        assert required_permission(tool) == PERMISSION_WRITE

    @pytest.mark.asyncio
    async def test_writes_atomically(self, sandbox: Path):
        target = sandbox / "out.txt"
        tool = create_fs_write_tool(allowed_roots=[sandbox])
        result = await tool.ainvoke({"path": str(target), "content": "hello"})
        assert "Wrote" in result
        assert target.read_text(encoding="utf-8") == "hello"

    @pytest.mark.asyncio
    async def test_refuses_path_outside_root(self, sandbox: Path, tmp_path_factory):
        outside = tmp_path_factory.mktemp("other") / "x.txt"
        tool = create_fs_write_tool(allowed_roots=[sandbox])
        result = await tool.ainvoke({"path": str(outside), "content": "no"})
        assert result.startswith("Error:")
        assert "outside" in result

    @pytest.mark.asyncio
    async def test_refuses_oversize_payload(self, sandbox: Path):
        target = sandbox / "big.txt"
        tool = create_fs_write_tool(allowed_roots=[sandbox], max_write_bytes=16)
        result = await tool.ainvoke({"path": str(target), "content": "x" * 32})
        assert result.startswith("Error:")
        assert "limit" in result
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_refuses_missing_parent_without_create_dirs(self, sandbox: Path):
        target = sandbox / "nested" / "deep.txt"
        tool = create_fs_write_tool(allowed_roots=[sandbox])
        result = await tool.ainvoke({"path": str(target), "content": "x"})
        assert result.startswith("Error:")
        assert "parent directory" in result

    @pytest.mark.asyncio
    async def test_creates_parents_when_requested(self, sandbox: Path):
        target = sandbox / "nested" / "deep.txt"
        tool = create_fs_write_tool(allowed_roots=[sandbox])
        result = await tool.ainvoke({"path": str(target), "content": "x", "create_dirs": True})
        assert "Wrote" in result
        assert target.read_text(encoding="utf-8") == "x"

    @pytest.mark.asyncio
    async def test_overwrites_existing_file_atomically(self, sandbox: Path):
        target = sandbox / "out.txt"
        target.write_text("original", encoding="utf-8")
        tool = create_fs_write_tool(allowed_roots=[sandbox])
        await tool.ainvoke({"path": str(target), "content": "replaced"})
        assert target.read_text(encoding="utf-8") == "replaced"
        # No leftover temp files.
        leftovers = [p.name for p in sandbox.iterdir() if p.name.startswith(".alex.")]
        assert leftovers == []
