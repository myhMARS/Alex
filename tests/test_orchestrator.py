"""Tests for TurnOrchestrator — user turn streaming."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from alex.agent.orchestrator import TurnOrchestrator, TurnResult
from alex.bus.events import (
    ThinkingUpdated,
    TokenEmitted,
    ToolStarted,
    ToolFinished,
    TurnCompleted,
    TurnStarted,
)


def _make_orchestrator(session_id: str = "s1"):
    import asyncio

    llm = MagicMock()
    memory = MagicMock()
    memory.get_context = AsyncMock(return_value=[HumanMessage(content="Hi"), AIMessage(content="Done")])
    memory.add_messages = AsyncMock()
    skill_manager = MagicMock()
    skill_manager.get_skill_by_name = MagicMock(return_value=None)
    push = MagicMock()
    turn_lock = asyncio.Lock()
    return TurnOrchestrator(
        llm=llm,
        memory=memory,
        skill_manager=skill_manager,
        push_notification=push,
        turn_lock=turn_lock,
        session_id=session_id,
    )


def _make_graph(events: list):
    """Create a mock graph whose astream_events yields the given events."""

    async def _stream(input, config, version):
        for ev in events:
            yield ev

    graph = MagicMock()
    graph.astream_events = _stream
    return graph


class TestTurnOrchestrator:
    @pytest.mark.asyncio
    async def test_streams_tokens(self):
        orch = _make_orchestrator()
        graph = _make_graph([
            {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="Hello")}},
            {"event": "on_chat_model_end", "data": {"output": AIMessage(content="Hello")}},
        ])

        events = []
        async for ev in orch.run("Hi", graph):
            events.append(ev)

        tokens = [e for e in events if isinstance(e, TokenEmitted)]
        assert len(tokens) == 1
        assert tokens[0].delta == "Hello"

    @pytest.mark.asyncio
    async def test_publishes_turn_started(self):
        orch = _make_orchestrator()
        graph = _make_graph([
            {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="x")}},
            {"event": "on_chat_model_end", "data": {"output": AIMessage(content="x")}},
        ])

        async for _ in orch.run("Hi", graph):
            pass

        calls = orch._push_notification.call_args_list
        assert len(calls) >= 2
        assert isinstance(calls[0][0][0], TurnStarted)
        # Last non-StreamEvent call should be TurnCompleted
        last_domain = None
        for call in calls:
            if isinstance(call[0][0], TurnCompleted):
                last_domain = call[0][0]
        assert last_domain is not None

    @pytest.mark.asyncio
    async def test_collects_thinking(self):
        orch = _make_orchestrator()

        class _ThinkingChunk(AIMessage):
            pass

        chunk = _ThinkingChunk(content="")
        chunk.additional_kwargs = {"reasoning_content": "Let me think..."}

        graph = _make_graph([
            {"event": "on_chat_model_stream", "data": {"chunk": chunk}},
            {"event": "on_chat_model_end", "data": {"output": AIMessage(content="Answer")}},
        ])

        thinking_events = []
        async for ev in orch.run("Hi", graph):
            if isinstance(ev, ThinkingUpdated):
                thinking_events.append(ev)

        assert len(thinking_events) == 1
        assert thinking_events[0].delta == "Let me think..."

    @pytest.mark.asyncio
    async def test_persists_message_batch(self):
        orch = _make_orchestrator()
        graph = _make_graph([
            {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="Done")}},
            {"event": "on_chat_model_end", "data": {"output": AIMessage(content="Done")}},
        ])

        async for _ in orch.run("Hi", graph):
            pass

        orch._memory.add_messages.assert_called_once()
        batch = orch._memory.add_messages.call_args[0][0]
        assert len(batch) >= 1
        assert batch[0].content == "Hi"  # HumanMessage

    @pytest.mark.asyncio
    async def test_last_result_has_message_batch(self):
        orch = _make_orchestrator()
        graph = _make_graph([
            {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="Done")}},
            {"event": "on_chat_model_end", "data": {"output": AIMessage(content="Done")}},
        ])

        async for _ in orch.run("Hi", graph):
            pass

        result = orch.last_result
        assert isinstance(result, TurnResult)
        assert len(result.message_batch) >= 1
        assert result.message_batch[0].content == "Hi"

    @pytest.mark.asyncio
    async def test_last_result_after_stream(self):
        orch = _make_orchestrator()
        graph = _make_graph([
            {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="Done")}},
            {"event": "on_chat_model_end", "data": {"output": AIMessage(content="Done")}},
        ])

        async for _ in orch.run("Hi", graph):
            pass

        result = orch.last_result
        assert isinstance(result, TurnResult)
        assert result.content == "Done"
        assert len(result.messages) >= 1

    @pytest.mark.asyncio
    async def test_tracks_tool_calls(self):
        orch = _make_orchestrator()
        graph = _make_graph([
            {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="Searching...")}},
            {"event": "on_tool_start", "name": "web_search", "run_id": "r1",
             "data": {"input": {"query": "test"}}},
            {"event": "on_chat_model_end", "data": {"output": AIMessage(content="Searching...", tool_calls=[{"name": "web_search", "args": {"query": "test"}, "id": "r1"}])}},
            {"event": "on_tool_end", "run_id": "r1",
             "data": {"output": "Results found"}},
            {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="Final")}},
            {"event": "on_chat_model_end", "data": {"output": AIMessage(content="Final")}},
        ])

        events = []
        async for ev in orch.run("Hi", graph):
            events.append(ev)

        tool_start_events = [e for e in events if isinstance(e, ToolStarted)]
        tool_end_events = [e for e in events if isinstance(e, ToolFinished)]
        assert len(tool_start_events) >= 1
        assert len(tool_end_events) >= 1

    @pytest.mark.asyncio
    async def test_set_session_id(self):
        orch = _make_orchestrator(session_id="old")
        orch.set_session_id("new")
        assert orch._session_id == "new"

    def test_turn_result_defaults(self):
        tr = TurnResult()
        assert tr.content == ""
        assert tr.thinking == ""
        assert tr.loaded_skill_ids == []
        assert tr.tool_names == []
        assert tr.last_query_matched is False


class TestEnsureReasoningRoundtrip:
    def test_adds_reasoning_content_to_missing(self):
        from alex.agent.orchestrator import _ensure_reasoning_roundtrip
        msg = AIMessage(content="test")
        # AIMessage with no additional_kwargs
        _ensure_reasoning_roundtrip([msg])
        assert msg.additional_kwargs == {"reasoning_content": ""}

    def test_adds_reasoning_content_to_empty_dict(self):
        from alex.agent.orchestrator import _ensure_reasoning_roundtrip
        msg = AIMessage(content="test", additional_kwargs={})
        _ensure_reasoning_roundtrip([msg])
        assert msg.additional_kwargs == {"reasoning_content": ""}

    def test_preserves_existing_reasoning(self):
        from alex.agent.orchestrator import _ensure_reasoning_roundtrip
        msg = AIMessage(content="test", additional_kwargs={"reasoning_content": "existing"})
        _ensure_reasoning_roundtrip([msg])
        assert msg.additional_kwargs["reasoning_content"] == "existing"

    def test_skips_non_ai_messages(self):
        from alex.agent.orchestrator import _ensure_reasoning_roundtrip
        msg = HumanMessage(content="test")
        _ensure_reasoning_roundtrip([msg])
        assert not hasattr(msg, "additional_kwargs") or not msg.additional_kwargs
