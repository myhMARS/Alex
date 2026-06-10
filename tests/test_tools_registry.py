"""Tests for ToolRegistry."""

import pytest
from pydantic import BaseModel, Field

from alex.kernel.dto.tool import ToolExecutionContext
from alex.tools.models import AlexTool
from alex.tools.registry import ToolRegistry


class _EchoInput(BaseModel):
    text: str = Field(description="Text to echo")


async def _echo(text: str) -> str:
    return f"ECHO: {text}"


def _make_tool(name: str = "echo") -> AlexTool:
    return AlexTool.from_function(
        name=name,
        description="Echo tool",
        coroutine=_echo,
        args_schema=_EchoInput,
    )


class TestToolRegistry:
    def test_initial_empty(self):
        reg = ToolRegistry()
        assert reg.list() == []
        assert reg.get("nonexistent") is None

    def test_register_and_get(self):
        reg = ToolRegistry()
        tool = _make_tool()
        reg.register(tool)
        assert reg.get("echo") is tool
        assert len(reg.list()) == 1

    def test_register_multiple(self):
        reg = ToolRegistry()
        reg.register(_make_tool("a"))
        reg.register(_make_tool("b"))
        reg.register(_make_tool("c"))
        assert len(reg.list()) == 3
        assert reg.get("b").name == "b"

    def test_unregister(self):
        reg = ToolRegistry()
        tool = _make_tool()
        reg.register(tool)
        reg.unregister("echo")
        assert reg.get("echo") is None
        assert reg.list() == []

    def test_register_overwrites_same_name(self):
        reg = ToolRegistry()
        t1 = _make_tool("echo")
        t2 = _make_tool("echo")
        reg.register(t1)
        reg.register(t2)
        assert len(reg.list()) == 1
        assert reg.get("echo") is t2

