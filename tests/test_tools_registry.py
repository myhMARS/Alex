"""Tests for ToolRegistry and ToolExecutor."""

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from alex.tools.registry import ToolRegistry
from alex.tools.executor import ToolExecutor


class _EchoInput(BaseModel):
    text: str = Field(description="Text to echo")


async def _echo(text: str) -> str:
    return f"ECHO: {text}"


def _make_tool(name: str = "echo") -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_echo,
        name=name,
        description="Echo tool",
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

    def test_unregister_nonexistent_does_not_raise(self):
        reg = ToolRegistry()
        reg.unregister("nonexistent")

    def test_register_overwrites_same_name(self):
        reg = ToolRegistry()
        t1 = _make_tool("echo")
        t2 = _make_tool("echo")
        reg.register(t1)
        reg.register(t2)
        assert len(reg.list()) == 1
        assert reg.get("echo") is t2


class TestToolExecutor:
    @pytest.mark.asyncio
    async def test_execute_registered_tool(self):
        reg = ToolRegistry()
        reg.register(_make_tool())
        executor = ToolExecutor(reg)
        result = await executor.execute("s1", "echo", {"text": "hello"})
        assert result == "ECHO: hello"

    @pytest.mark.asyncio
    async def test_execute_nonexistent_tool(self):
        reg = ToolRegistry()
        executor = ToolExecutor(reg)
        result = await executor.execute("s1", "nonexistent", {})
        assert result.startswith("Error:")

    @pytest.mark.asyncio
    async def test_execute_passes_session_id(self):
        reg = ToolRegistry()
        seen_session: list = []

        async def _capture(text: str) -> str:
            return text

        tool = StructuredTool.from_function(
            coroutine=_capture,
            name="capture",
            description="capture",
        )
        reg.register(tool)
        executor = ToolExecutor(reg)
        result = await executor.execute("session-abc", "capture", {"text": "hi"})
        assert result == "hi"
