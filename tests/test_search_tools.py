"""Tests for the Grep and Glob tools.

Grep tests exercise the pure-Python fallback path so they run
deterministically regardless of whether ripgrep is installed on the
host.  We therefore monkeypatch ``_ripgrep_available`` to return
``False`` for the affected scope.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from alex.tools import search as search_module
from alex.tools.permissions import PERMISSION_READ, required_permission
from alex.tools.search import create_glob_tool, create_grep_tool


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "import os\n"
        "def hello():\n"
        "    print('hello world')\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "b.py").write_text(
        "from typing import Any\n"
        "def goodbye():\n"
        "    print('goodbye, hello cruel world')\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "# Demo\n\nSay HELLO to the project.\n",
        encoding="utf-8",
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.py").write_text(
        "hello = 'in node_modules — should be skipped by walker'\n",
        encoding="utf-8",
    )
    # binary file — must be ignored
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02hello\x03" * 64)
    return tmp_path


@pytest.fixture
def force_python_fallback(monkeypatch):
    monkeypatch.setattr(search_module, "_ripgrep_available", lambda: False)


# ── grep ──────────────────────────────────────────────────────────────


class TestGrepMetadata:
    def test_declares_read_permission(self):
        tool = create_grep_tool()
        assert tool.name == "grep"
        assert required_permission(tool) == PERMISSION_READ


class TestGrepFilesWithMatches:
    @pytest.mark.asyncio
    async def test_lists_matching_files(self, repo: Path, force_python_fallback):
        tool = create_grep_tool(allowed_roots=[repo])
        result = await tool.coroutine(pattern="hello", path=str(repo))
        assert "a.py" in result
        assert "b.py" in result
        assert "README.md" not in result  # case-sensitive by default
        # Pruned directory must not leak into results.
        assert "node_modules" not in result
        # Binary file must not leak.
        assert "blob.bin" not in result

    @pytest.mark.asyncio
    async def test_glob_filter(self, repo: Path, force_python_fallback):
        tool = create_grep_tool(allowed_roots=[repo])
        result = await tool.coroutine(pattern="hello", path=str(repo), glob="*.py")
        assert "a.py" in result
        assert "README.md" not in result

    @pytest.mark.asyncio
    async def test_type_filter(self, repo: Path, force_python_fallback):
        tool = create_grep_tool(allowed_roots=[repo])
        result = await tool.coroutine(pattern="hello", path=str(repo), type="md")
        # Case-sensitive search — README has HELLO not hello, so no match.
        assert "No matches" in result

        result_ci = await tool.coroutine(
            pattern="hello", path=str(repo), type="md", ignore_case=True,
        )
        assert "README.md" in result_ci

    @pytest.mark.asyncio
    async def test_no_matches(self, repo: Path, force_python_fallback):
        tool = create_grep_tool(allowed_roots=[repo])
        result = await tool.coroutine(pattern="zzz_nope_zzz", path=str(repo))
        assert "No matches" in result


class TestGrepContent:
    @pytest.mark.asyncio
    async def test_default_shows_line_numbers(self, repo: Path, force_python_fallback):
        tool = create_grep_tool(allowed_roots=[repo])
        result = await tool.coroutine(
            pattern="hello", path=str(repo / "src" / "a.py"),
            output_mode="content",
        )
        # Format: <path>:<line_no>:<text>
        assert ":3:" in result
        assert "hello world" in result

    @pytest.mark.asyncio
    async def test_context_lines(self, repo: Path, force_python_fallback):
        tool = create_grep_tool(allowed_roots=[repo])
        result = await tool.coroutine(
            pattern="hello", path=str(repo / "src" / "b.py"),
            output_mode="content", context=1,
        )
        assert "goodbye" in result  # before-context line
        assert "return 1" in result  # after-context line

    @pytest.mark.asyncio
    async def test_head_limit(self, repo: Path, force_python_fallback):
        tool = create_grep_tool(allowed_roots=[repo])
        # Head_limit smaller than the natural result count.
        result = await tool.coroutine(
            pattern="def ", path=str(repo), output_mode="content", head_limit=1,
        )
        lines = [line for line in result.splitlines() if ":" in line]
        # one truncation marker plus header expected
        assert any("truncated" in line for line in result.splitlines())
        assert len(lines) >= 1


class TestGrepCount:
    @pytest.mark.asyncio
    async def test_per_file_counts(self, repo: Path, force_python_fallback):
        tool = create_grep_tool(allowed_roots=[repo])
        result = await tool.coroutine(
            pattern="hello", path=str(repo / "src"), output_mode="count",
        )
        # a.py has "def hello" + "print('hello world')" = 2 hits
        assert "a.py:2" in result
        # b.py says "goodbye, hello cruel world" → 1 hit
        assert "b.py:1" in result


class TestGrepSafety:
    @pytest.mark.asyncio
    async def test_path_outside_allowed_roots(
        self, repo: Path, tmp_path_factory, force_python_fallback,
    ):
        outside = tmp_path_factory.mktemp("elsewhere")
        tool = create_grep_tool(allowed_roots=[repo])
        result = await tool.coroutine(pattern="hello", path=str(outside))
        assert result.startswith("Error:")
        assert "outside" in result

    @pytest.mark.asyncio
    async def test_invalid_regex(self, repo: Path, force_python_fallback):
        tool = create_grep_tool(allowed_roots=[repo])
        result = await tool.coroutine(pattern="[unclosed", path=str(repo))
        assert result.startswith("Error: invalid regex")

    @pytest.mark.asyncio
    async def test_invalid_output_mode(self, repo: Path, force_python_fallback):
        tool = create_grep_tool(allowed_roots=[repo])
        result = await tool.coroutine(
            pattern="hello", path=str(repo), output_mode="bogus",
        )
        assert result.startswith("Error:")


# ── glob ──────────────────────────────────────────────────────────────


@pytest.fixture
def mtime_repo(tmp_path: Path) -> Path:
    (tmp_path / "old.py").write_text("# old\n", encoding="utf-8")
    nested = tmp_path / "src"
    nested.mkdir()
    (nested / "new.py").write_text("# new\n", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("skip\n", encoding="utf-8")

    # Force distinct mtimes so the sort is deterministic.
    now = time.time()
    import os as _os
    _os.utime(tmp_path / "old.py", (now - 100, now - 100))
    _os.utime(nested / "new.py", (now, now))
    return tmp_path


class TestGlob:
    def test_metadata(self):
        tool = create_glob_tool()
        assert tool.name == "glob"
        assert required_permission(tool) == PERMISSION_READ

    @pytest.mark.asyncio
    async def test_lists_matching_files_sorted_by_mtime(self, mtime_repo: Path):
        tool = create_glob_tool(allowed_roots=[mtime_repo])
        result = await tool.coroutine(pattern="**/*.py", path=str(mtime_repo))
        assert "new.py" in result
        assert "old.py" in result
        assert "skip.txt" not in result
        # newest first
        new_idx = result.index("new.py")
        old_idx = result.index("old.py")
        assert new_idx < old_idx

    @pytest.mark.asyncio
    async def test_no_matches(self, mtime_repo: Path):
        tool = create_glob_tool(allowed_roots=[mtime_repo])
        result = await tool.coroutine(pattern="**/*.zz", path=str(mtime_repo))
        assert "No files match" in result

    @pytest.mark.asyncio
    async def test_path_outside_allowed_roots(self, mtime_repo: Path, tmp_path_factory):
        outside = tmp_path_factory.mktemp("other")
        tool = create_glob_tool(allowed_roots=[mtime_repo])
        result = await tool.coroutine(pattern="**/*.py", path=str(outside))
        assert result.startswith("Error:")
        assert "outside" in result

    @pytest.mark.asyncio
    async def test_default_path_is_first_root(self, mtime_repo: Path):
        # No path → defaults to the first allowed root.
        tool = create_glob_tool(allowed_roots=[mtime_repo])
        result = await tool.coroutine(pattern="**/*.py")
        assert "new.py" in result

    @pytest.mark.asyncio
    async def test_empty_pattern_rejected(self, mtime_repo: Path):
        tool = create_glob_tool(allowed_roots=[mtime_repo])
        result = await tool.coroutine(pattern="", path=str(mtime_repo))
        assert result.startswith("Error:")
