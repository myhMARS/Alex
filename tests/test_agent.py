"""Core tests for the Agent."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
pytest.importorskip("langchain_core")
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from alex.agent import Agent
from alex.bus import AsyncEventBus
from alex.bus.events import CronJobEvent, TokenEmitted


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


_BASE_TOOL_COUNT = 2  # built-in load_skill + cron_jobs


async def _consume_stream(agent, message: str) -> list:
    """Consume all events from chat_stream and return them."""
    events = []
    async for event in agent.chat_stream(message):
        events.append(event)
    return events


class TestAgentInit:
    def test_creates_without_tools(self):
        agent = Agent()
        assert len(agent.tools) == _BASE_TOOL_COUNT
        assert agent.history == []
        assert agent.get_tool("nonexistent") is None

    def test_creates_with_tools(self):
        tool = _make_test_tool()
        agent = Agent(tools=[tool])
        assert len(agent.tools) == _BASE_TOOL_COUNT + 1
        assert agent.get_tool("echo") is tool


class TestToolRegistry:
    def test_register(self):
        agent = Agent()
        tool = _make_test_tool()
        agent.register_tool(tool)
        assert len(agent.tools) == _BASE_TOOL_COUNT + 1
        assert agent.get_tool("echo") is tool

    def test_unregister(self):
        agent = Agent()
        tool = _make_test_tool()
        agent.register_tool(tool)
        agent.unregister_tool("echo")
        assert len(agent.tools) == _BASE_TOOL_COUNT
        assert agent.get_tool("echo") is None

    def test_unregister_nonexistent_does_not_raise(self):
        agent = Agent()
        agent.unregister_tool("nonexistent")

    @pytest.mark.asyncio
    async def test_push_notification_accepts_cron_job_event_without_subscribe_field(self):
        agent = Agent()
        bus = MagicMock()
        agent.bind_event_bus(bus)

        event = CronJobEvent(job_id="job-1", name="daily", status="SUCCESS")
        await agent.push_notification(event)

        bus.publish.assert_called_once_with(event)


class TestHistory:
    @pytest.mark.asyncio
    async def test_clear_history(self):
        agent = Agent()
        with patch.object(agent._chat._prompt, "ensure_skills_prompt", return_value=False):
            with patch.object(agent._feedback, "maybe_reflect", new_callable=AsyncMock):
                with patch.object(agent._chat, "_graph") as mock_graph:
                    mock_stream = mock_graph.astream_events
                    async def _events():
                        yield {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="Hello!")}}
                        yield {"event": "on_chat_model_end", "data": {"output": AIMessage(content="Hello!")}}

                    mock_stream.return_value = _events()
                    await _consume_stream(agent, "Hi")
                    assert len(agent.history) >= 1
                    await agent.clear_history()
                    assert len(agent.history) == 0


class TestChatStream:
    @pytest.mark.asyncio
    async def test_returns_response(self):
        agent = Agent()
        with patch.object(agent._chat._prompt, "ensure_skills_prompt", return_value=False):
            with patch.object(agent._feedback, "maybe_reflect", new_callable=AsyncMock):
                with patch.object(agent._chat, "_graph") as mock_graph:
                    mock_stream = mock_graph.astream_events
                    async def _events():
                        yield {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="Hello, I'm Alex.")}}
                        yield {"event": "on_chat_model_end", "data": {"output": AIMessage(content="Hello, I'm Alex.")}}

                    mock_stream.return_value = _events()
                    collected = []
                    async for event in agent.chat_stream("Hi"):
                        if isinstance(event, TokenEmitted):
                            collected.append(event.delta)
                    response = "".join(collected)
                    assert response == "Hello, I'm Alex."
                    hist = agent.history
                    assert len(hist) >= 1
                    assert hist[-1].content == "Hello, I'm Alex."

    @pytest.mark.asyncio
    async def test_passes_chat_history(self):
        agent = Agent()
        with patch.object(agent._chat._prompt, "ensure_skills_prompt", return_value=False):
            with patch.object(agent._feedback, "maybe_reflect", new_callable=AsyncMock):
                with patch.object(agent._chat, "_graph") as mock_graph:
                    mock_stream = mock_graph.astream_events
                    async def _events1():
                        yield {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="Replying")}}
                        yield {"event": "on_chat_model_end", "data": {"output": AIMessage(content="Replying")}}

                    mock_stream.return_value = _events1()
                    await _consume_stream(agent, "First")

                    async def _events2():
                        yield {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="Second reply")}}
                        yield {"event": "on_chat_model_end", "data": {"output": AIMessage(content="Second reply")}}

                    mock_stream.return_value = _events2()
                    await _consume_stream(agent, "Second")

                    assert mock_stream.call_count == 2
                    second_call = mock_stream.call_args_list[1][0][0]
                    msgs = second_call["messages"]
                    assert len(msgs) >= 2  # includes previous messages + new HumanMessage
                    assert msgs[-1].content == "Second"

    @pytest.mark.asyncio
    async def test_user_turn_streams_via_bus_when_bus_is_bound(self):
        agent = Agent()
        bus = AsyncEventBus()
        await bus.start()
        agent.bind_event_bus(bus)
        seen_bus_tokens: list[str] = []

        async def _on_token(event: TokenEmitted) -> None:
            seen_bus_tokens.append(event.delta)

        await bus.subscribe(TokenEmitted, _on_token)

        try:
            with patch.object(agent._chat._prompt, "ensure_skills_prompt", return_value=False):
                with patch.object(agent._feedback, "maybe_reflect", new_callable=AsyncMock):
                    with patch.object(agent._chat, "_graph") as mock_graph:
                        mock_stream = mock_graph.astream_events

                        async def _events():
                            yield {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="bus-path")}}
                            yield {"event": "on_chat_model_end", "data": {"output": AIMessage(content="bus-path")}}

                        mock_stream.return_value = _events()
                        collected = []
                        async for event in agent.chat_stream("Hi"):
                            if isinstance(event, TokenEmitted):
                                collected.append(event.delta)
        finally:
            await bus.shutdown()

        assert "".join(collected) == "bus-path"
        assert "".join(seen_bus_tokens) == "bus-path"

    @pytest.mark.asyncio
    async def test_user_and_cron_turns_share_single_fifo_queue(self):
        agent = Agent()

        order: list[str] = []
        started_second_user = asyncio.Event()

        async def _stream(input_data, config, version):
            message = input_data["messages"][-1].content
            order.append(message)
            if message == "first":
                await asyncio.sleep(0.05)
            if message == "second":
                started_second_user.set()
            yield {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content=f"reply:{message}")}}
            yield {"event": "on_chat_model_end", "data": {"output": AIMessage(content=f"reply:{message}")}}

        with patch.object(agent._chat._prompt, "ensure_skills_prompt", return_value=False):
            with patch.object(agent._feedback, "maybe_reflect", new_callable=AsyncMock):
                graph = MagicMock()
                graph.astream_events = _stream
                agent._chat._graph = graph

                first_task = asyncio.create_task(_consume_stream(agent, "first"))
                await asyncio.sleep(0.01)
                cron_task = asyncio.create_task(agent.execute_cron_prompt(
                    session_id=agent.session_id,
                    job_id="job-1",
                    name="cron",
                    prompt="cron-prompt",
                    stream_id="cron:1",
                ))
                await asyncio.sleep(0.01)
                second_task = asyncio.create_task(_consume_stream(agent, "second"))

                await asyncio.gather(first_task, cron_task, second_task)

        assert started_second_user.is_set()
        assert order == ["first", "cron-prompt", "second"]
