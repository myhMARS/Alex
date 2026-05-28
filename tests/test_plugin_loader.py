"""Tests for the user plugin loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from alex.tools.plugin_loader import (
    discover_plugin_files,
    install_plugins,
    load_plugins,
)


PLUGIN_TOOLS_LIST = '''
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


class _Input(BaseModel):
    text: str = Field(default="ok")


async def _run(text: str = "ok") -> str:
    return text


ALEX_TOOLS = [
    StructuredTool.from_function(
        coroutine=_run,
        name="plugin_echo",
        description="echo",
        args_schema=_Input,
    ),
]
'''


PLUGIN_TOOLS_FACTORY = '''
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


class _Input(BaseModel):
    text: str = Field(default="ok")


async def _run(text: str = "ok") -> str:
    return text


def tools():
    return [
        StructuredTool.from_function(
            coroutine=_run,
            name="plugin_factory_echo",
            description="echo",
            args_schema=_Input,
        ),
    ]
'''


PLUGIN_REGISTER = '''
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


class _Input(BaseModel):
    text: str = Field(default="ok")


async def _run(text: str = "ok") -> str:
    return text


def register(agent):
    agent.register_tool(
        StructuredTool.from_function(
            coroutine=_run,
            name="plugin_register_echo",
            description="echo",
            args_schema=_Input,
        )
    )
'''


PLUGIN_BROKEN = '''
import nonexistent_module_xyz
'''


PLUGIN_NOOP = '''
# no entrypoint defined
'''


class _StubAgent:
    def __init__(self) -> None:
        self.registered: list = []

    def register_tool(self, tool) -> None:
        self.registered.append(tool)


@pytest.fixture
def plugin_root(tmp_path: Path) -> Path:
    return tmp_path


def _write_plugin(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.write_text(content, encoding="utf-8")
    return path


class TestDiscover:
    def test_discover_skips_underscore(self, plugin_root: Path):
        _write_plugin(plugin_root, "_helpers.py", "# private")
        _write_plugin(plugin_root, "real.py", PLUGIN_TOOLS_LIST)
        files = discover_plugin_files(plugin_root)
        assert [p.name for p in files] == ["real.py"]

    def test_discover_returns_empty_when_root_missing(self, tmp_path: Path):
        nonexistent = tmp_path / "nope"
        assert discover_plugin_files(nonexistent) == []


class TestLoadPlugins:
    def test_alex_tools_constant(self, plugin_root: Path):
        _write_plugin(plugin_root, "list.py", PLUGIN_TOOLS_LIST)
        results = load_plugins(root=plugin_root)
        assert len(results) == 1
        result = results[0]
        assert result.ok
        assert result.registered_via == "ALEX_TOOLS"
        assert [t.name for t in result.tools] == ["plugin_echo"]

    def test_tools_factory(self, plugin_root: Path):
        _write_plugin(plugin_root, "factory.py", PLUGIN_TOOLS_FACTORY)
        results = load_plugins(root=plugin_root)
        assert len(results) == 1
        result = results[0]
        assert result.ok
        assert result.registered_via == "tools()"
        assert [t.name for t in result.tools] == ["plugin_factory_echo"]

    def test_register_callback_requires_agent(self, plugin_root: Path):
        _write_plugin(plugin_root, "register.py", PLUGIN_REGISTER)
        results = load_plugins(root=plugin_root, agent=None)
        assert len(results) == 1
        assert not results[0].ok
        assert "register" in results[0].error.lower()

    def test_register_callback_runs_with_agent(self, plugin_root: Path):
        _write_plugin(plugin_root, "register.py", PLUGIN_REGISTER)
        agent = _StubAgent()
        results = load_plugins(root=plugin_root, agent=agent)
        assert len(results) == 1
        assert results[0].ok
        assert results[0].registered_via == "register()"
        assert [t.name for t in agent.registered] == ["plugin_register_echo"]

    def test_broken_plugin_isolated(self, plugin_root: Path):
        _write_plugin(plugin_root, "broken.py", PLUGIN_BROKEN)
        _write_plugin(plugin_root, "list.py", PLUGIN_TOOLS_LIST)
        results = load_plugins(root=plugin_root)
        assert len(results) == 2
        broken = next(r for r in results if r.path.name == "broken.py")
        ok = next(r for r in results if r.path.name == "list.py")
        assert not broken.ok
        assert ok.ok

    def test_noop_plugin_reports_missing_entrypoint(self, plugin_root: Path):
        _write_plugin(plugin_root, "noop.py", PLUGIN_NOOP)
        results = load_plugins(root=plugin_root)
        assert len(results) == 1
        assert not results[0].ok
        assert "ALEX_TOOLS" in results[0].error or "register" in results[0].error


class TestInstallPlugins:
    def test_register_tools_via_agent(self, plugin_root: Path):
        _write_plugin(plugin_root, "list.py", PLUGIN_TOOLS_LIST)
        agent = _StubAgent()
        results = install_plugins(agent, root=plugin_root)
        assert len(results) == 1
        assert results[0].ok
        assert [t.name for t in agent.registered] == ["plugin_echo"]
