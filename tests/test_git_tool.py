"""Tests for the git_inspect tool — uses an isolated tmp git repo."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from alex.tools.git import create_git_inspect_tool
from alex.tools.permissions import PERMISSION_READ, required_permission


_GIT_AVAILABLE = shutil.which("git") is not None


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if not _GIT_AVAILABLE:
        pytest.skip("git not installed")

    async def _setup() -> None:
        commands = [
            ["git", "init", "--quiet", "-b", "main"],
            ["git", "config", "user.email", "alex@example.com"],
            ["git", "config", "user.name", "alex"],
        ]
        for argv in commands:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(tmp_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
        for argv in [
            ["git", "add", "README.md"],
            ["git", "commit", "-m", "init", "--quiet"],
        ]:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(tmp_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

    asyncio.run(_setup())
    return tmp_path


class TestGitInspect:
    def test_metadata_declares_read_permission(self):
        tool = create_git_inspect_tool()
        assert tool.name == "git_inspect"
        assert required_permission(tool) == PERMISSION_READ

    @pytest.mark.asyncio
    async def test_status_clean(self, repo: Path):
        tool = create_git_inspect_tool(allowed_roots=[repo])
        result = await tool.ainvoke({"action": "status", "path": str(repo)})
        assert "exit_code: 0" in result
        assert "main" in result

    @pytest.mark.asyncio
    async def test_log_lists_recent_commits(self, repo: Path):
        tool = create_git_inspect_tool(allowed_roots=[repo])
        result = await tool.ainvoke({"action": "log", "path": str(repo), "max_count": 5})
        assert "exit_code: 0" in result
        assert "init" in result

    @pytest.mark.asyncio
    async def test_diff_shows_pending_changes(self, repo: Path):
        (repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")
        tool = create_git_inspect_tool(allowed_roots=[repo])
        result = await tool.ainvoke({"action": "diff", "path": str(repo)})
        assert "exit_code: 0" in result

    @pytest.mark.asyncio
    async def test_refuses_path_outside_allowed_roots(self, repo: Path, tmp_path_factory):
        outside = tmp_path_factory.mktemp("other")
        tool = create_git_inspect_tool(allowed_roots=[repo])
        result = await tool.ainvoke({"action": "status", "path": str(outside)})
        assert result.startswith("Error:")
        assert "outside" in result
