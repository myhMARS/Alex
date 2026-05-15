"""Core tests for the Agent."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from alex.agent import Agent


class _TestInput(BaseModel):
    text: str = Field(description="Some text")


async def _test_echo(text: str) -> str:
    return f"ECHO: {text}"


def _make_test_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_test_echo,
        name="echo",
        description="Echo text back",
        args_schema=_TestInput,
    )


class TestAgentInit:
    def test_creates_without_tools(self):
        agent = Agent()
        assert agent.tools == []
        assert agent.history == []
        assert agent.get_tool("nonexistent") is None

    def test_creates_with_tools(self):
        tool = _make_test_tool()
        agent = Agent(tools=[tool])
        assert len(agent.tools) == 1
        assert agent.get_tool("echo") is tool


class TestToolRegistry:
    def test_register(self):
        agent = Agent()
        tool = _make_test_tool()
        agent.register_tool(tool)
        assert len(agent.tools) == 1
        assert agent.get_tool("echo") is tool

    def test_unregister(self):
        agent = Agent()
        tool = _make_test_tool()
        agent.register_tool(tool)
        agent.unregister_tool("echo")
        assert len(agent.tools) == 0
        assert agent.get_tool("echo") is None

    def test_unregister_nonexistent_does_not_raise(self):
        agent = Agent()
        agent.unregister_tool("nonexistent")


class TestHistory:
    @pytest.mark.asyncio
    async def test_clear_history(self):
        agent = Agent()
        with patch.object(agent._graph, "ainvoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = {"messages": [
                HumanMessage(content="Hi"),
                AIMessage(content="IGNORED", tool_calls=[]),
                AIMessage(content="Hello!"),
            ]}
            await agent.chat("Hi")
            assert len(agent.history) == 3
            await agent.clear_history()
            assert len(agent.history) == 0


class TestChat:
    @pytest.mark.asyncio
    async def test_returns_response(self):
        agent = Agent()
        with patch.object(agent._graph, "ainvoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = {"messages": [
                HumanMessage(content="Hi"),
                AIMessage(content="IGNORED", tool_calls=[]),
                AIMessage(content="Hello, I'm Alex."),
            ]}
            response = await agent.chat("Hi")
            assert response == "Hello, I'm Alex."
            hist = agent.history
            assert len(hist) == 3
            assert hist[0].content == "Hi"
            assert hist[2].content == "Hello, I'm Alex."

    @pytest.mark.asyncio
    async def test_passes_chat_history(self):
        agent = Agent()
        with patch.object(agent._graph, "ainvoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = {"messages": [
                HumanMessage(content="First"),
                AIMessage(content="IGNORED", tool_calls=[]),
                AIMessage(content="Replying"),
            ]}
            await agent.chat("First")

            mock_invoke.return_value = {"messages": [
                HumanMessage(content="First"),
                AIMessage(content="IGNORED", tool_calls=[]),
                AIMessage(content="Replying"),
                HumanMessage(content="Second"),
                AIMessage(content="IGNORED2", tool_calls=[]),
                AIMessage(content="Second reply"),
            ]}
            await agent.chat("Second")

            assert mock_invoke.call_count == 2
            second_call = mock_invoke.call_args_list[1][0][0]
            msgs = second_call["messages"]
            assert len(msgs) == 4  # 3 from first turn + new HumanMessage
            assert msgs[-1].content == "Second"
