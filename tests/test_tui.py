"""Regression tests for TUI rendering and session lifecycle."""

import pytest
pytest.importorskip("textual")
from textual.app import App, ComposeResult
from textual.widgets import Input

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from alex.tui import (
    AlexApp,
    AlexBubble,
    ChatHistory,
    ChatTurn,
    ToolBubble,
    _messages_to_turns,
)
import alex.session as session_store


class _BubbleHarness(App[None]):
    def __init__(self, bubble: AlexBubble) -> None:
        super().__init__()
        self._bubble = bubble

    def compose(self) -> ComposeResult:
        yield self._bubble


@pytest.mark.asyncio
async def test_finalize_renders_tool_calls_before_response():
    bubble = AlexBubble()
    turn = ChatTurn(
        user_input="现在几点",
        response="现在是 2026 年 5 月 15 日下午 5 点 33 分。",
        thinking="先调用时间工具。",
        tool_calls=[
            {
                "name": "time",
                "args": {"timezone": "Asia/Shanghai"},
                "output": "Current time: 2026-05-15 17:33:53 中国标准时间",
            }
        ],
    )

    async with _BubbleHarness(bubble).run_test() as pilot:
        bubble.finalize(turn)
        await pilot.pause()

        children = list(bubble.children)
        tool_index = next(i for i, child in enumerate(children) if isinstance(child, ToolBubble))
        response_index = next(
            i for i, child in enumerate(children)
            if "response-text" in getattr(child, "classes", set())
        )

        assert tool_index < response_index


@pytest.mark.asyncio
async def test_insert_tool_removes_pre_tool_response_from_top():
    bubble = AlexBubble()

    async with _BubbleHarness(bubble).run_test() as pilot:
        bubble.set_response("好的，我来查一下当前时间。")
        bubble.insert_tool("time", {"timezone": "Asia/Shanghai"})
        await pilot.pause()

        children = list(bubble.children)

        assert isinstance(children[0], ToolBubble)
        assert "response-text" in getattr(children[-1], "classes", set())


class _AgentStub:
    def __init__(self, notes: list) -> None:
        self._notes = list(notes)
        self._history: list[BaseMessage] = []
        self._cron_history: list[dict] = []
        self._session_id: str = ""

    def pop_notifications(self) -> list:
        notes = self._notes[:]
        self._notes.clear()
        return notes

    async def restore_history(self, messages: list) -> None:
        self._history = list(messages)

    async def clear_history(self) -> None:
        self._history.clear()

    def set_session_context(self, session_id: str, cron_history: list[dict] | None = None) -> None:
        self._session_id = session_id
        self._cron_history = list(cron_history or [])

    def list_session_cron_history(self, query: str = "", limit: int = 20) -> list[dict]:
        return list(self._cron_history)[:limit]

    @property
    def history(self) -> list:
        return self._history


@pytest.mark.asyncio
async def test_reflect_notification_shows_toast():
    from alex.events import SkillReflectEvent
    agent = _AgentStub([SkillReflectEvent(new=1, updated=0, deprecated=0, names=["foo"])])
    app = AlexApp(agent)

    async with app.run_test() as pilot:
        pilot.app._poll_notifications()
        await pilot.pause()
        toasts = list(pilot.app.query(".toast"))
        assert len(toasts) >= 1
        visible = [t for t in toasts if "toast-hidden" not in getattr(t, "classes", set())]
        assert len(visible) >= 1


def test_reflect_event_shows_updated_skill_names():
    from alex.events import SkillReflectEvent

    evt = SkillReflectEvent(updated=1, updated_names=["Timer Reminder"])
    assert evt.toast == "Skills refined — 1 updated: Timer Reminder"


# ── session lifecyle regression tests ─────────────────────────────────────

@pytest.mark.asyncio
async def test_input_widget_id_correct():
    """Regression: #input-box exists; the wrong #user-input would crash."""
    app = AlexApp(_AgentStub([]))

    async with app.run_test() as pilot:
        # Verify the correct ID works
        input_widget = pilot.app.query_one("#input-box", Input)
        assert input_widget is not None

        # Verify the wrong ID raises (would have caused the crash)
        from textual.css.query import NoMatches
        with pytest.raises(NoMatches):
            pilot.app.query_one("#user-input", Input)


@pytest.mark.asyncio
async def test_resume_restores_agent_memory():
    """Session resume restores agent memory with exact message sequence."""
    session_id = "test_resume_memory"
    messages: list[BaseMessage] = [
        HumanMessage(content="Hello"),
        AIMessage(content="", tool_calls=[{"name": "time", "args": {}, "id": "c1"}]),
        ToolMessage(content="10:00 UTC", tool_call_id="c1"),
        AIMessage(content="It is 10 AM.", additional_kwargs={"reasoning_content": "got time"}),
    ]
    session_store.save_session(session_id, messages)

    agent = _AgentStub([])
    app = AlexApp(agent)

    async with app.run_test() as pilot:
        # Use the worker API: schedule work, wait for it via pilot.pause polling
        worker = app._resume_session(session_id)
        # Poll until worker completes
        for _ in range(50):
            await pilot.pause(0.05)
            if worker.is_finished:
                break

        assert worker.is_finished
        assert len(agent.history) == len(messages)
        assert agent.history[0].content == "Hello"
        assert agent.history[3].content == "It is 10 AM."

        # Verify input is re-enabled after resume
        input_widget = pilot.app.query_one("#input-box", Input)
        assert not input_widget.disabled


@pytest.mark.asyncio
async def test_clear_clears_agent_memory():
    """Session clear removes agent memory and leaves input enabled."""
    session_id = "test_clear_memory"
    session_store.save_session(session_id, [
        HumanMessage(content="Hi"),
        AIMessage(content="Hey!"),
    ])

    agent = _AgentStub([])
    app = AlexApp(agent)

    async with app.run_test() as pilot:
        # Load session first
        w_load = app._resume_session(session_id)
        for _ in range(50):
            await pilot.pause(0.05)
            if w_load.is_finished:
                break
        assert len(agent.history) > 0

        # Clear
        w_clear = app._clear_chat()
        for _ in range(50):
            await pilot.pause(0.05)
            if w_clear.is_finished:
                break

        assert w_clear.is_finished
        assert len(agent.history) == 0

        input_widget = pilot.app.query_one("#input-box", Input)
        assert not input_widget.disabled


# ── ChatHistory message-sequence fidelity ──────────────────────────────────

def test_chat_history_preserves_loaded_messages():
    """Loaded messages survive save/load round-trip without degradation."""
    session_id = "test_fidelity"
    original: list[BaseMessage] = [
        HumanMessage(content="What time is it?"),
        AIMessage(content="", tool_calls=[{"name": "time", "args": {"tz": "UTC"}, "id": "call_1"}]),
        ToolMessage(content="2026-05-20 10:00:00", tool_call_id="call_1"),
        AIMessage(content="It is 10 AM UTC.", additional_kwargs={"reasoning_content": "check"}),
    ]
    session_store.save_session(session_id, original)

    # Load → add a new turn with its exact message delta → save → reload
    h1 = ChatHistory(session_id=session_id)
    assert h1.load()
    new_delta: list[BaseMessage] = [
        HumanMessage(content="Thanks"),
        AIMessage(content="You're welcome!"),
    ]
    h1.add(ChatTurn(user_input="Thanks", response="You're welcome!"), messages_delta=new_delta)

    # Reload
    h2 = ChatHistory(session_id=session_id)
    assert h2.load()

    assert len(h2.turns) == 2
    assert h2.turns[0].user_input == "What time is it?"
    assert h2.turns[0].response == "It is 10 AM UTC."
    assert h2.turns[0].thinking == "check"
    assert len(h2.turns[0].tool_calls) == 1
    assert h2.turns[0].tool_calls[0]["name"] == "time"

    # The loaded messages must match the original + new delta exactly
    msgs = h2.loaded_messages
    assert len(msgs) == len(original) + len(new_delta)
    assert msgs[0].content == "What time is it?"
    assert msgs[3].content == "It is 10 AM UTC."
    assert msgs[4].content == "Thanks"
    assert msgs[5].content == "You're welcome!"


def test_chat_history_empty_session_is_valid():
    """An empty message list (after clear) is a valid loadable session."""
    session_id = "test_empty"
    session_store.save_session(session_id, [])

    h = ChatHistory(session_id=session_id)
    assert h.load() is True
    assert len(h.turns) == 0
    assert len(h.loaded_messages) == 0
    assert len(h.cron_history) == 0


def test_chat_history_persists_cron_records():
    session_id = "test_cron_history"
    session_store.save_session_bundle(session_id, [], [{
        "execution_id": "cron:job:1",
        "job_id": "job",
        "name": "提醒",
        "status": "SUCCESS",
        "action": "notify",
        "params": {"message": "hello"},
        "started_at": 1.0,
        "finished_at": 2.0,
        "result": "hello",
        "error": "",
    }])

    h = ChatHistory(session_id=session_id)
    assert h.load() is True
    assert len(h.cron_history) == 1
    assert h.cron_history[0]["name"] == "提醒"


# ── _messages_to_turns multi-tool correctness ──────────────────────────────

def test_messages_to_turns_multi_tool():
    """A single AIMessage with multiple tool_calls must pair correctly."""
    messages: list[BaseMessage] = [
        HumanMessage(content="Search and fetch"),
        AIMessage(content="", tool_calls=[
            {"name": "search", "args": {"q": "X"}, "id": "t1"},
            {"name": "fetch", "args": {"url": "Y"}, "id": "t2"},
        ]),
        ToolMessage(content="search results", tool_call_id="t1"),
        ToolMessage(content="fetch result", tool_call_id="t2"),
        AIMessage(content="Done."),
    ]

    turns, msgs = _messages_to_turns(messages)
    assert len(turns) == 1
    turn = turns[0]
    assert turn.user_input == "Search and fetch"
    assert turn.response == "Done."
    assert len(turn.tool_calls) == 2

    # t1 → "search" with its output
    t1 = turn.tool_calls[0]
    assert t1["name"] == "search"
    assert t1["output"] == "search results"

    # t2 → "fetch" with its output (not lost to single-pointer bug)
    t2 = turn.tool_calls[1]
    assert t2["name"] == "fetch"
    assert t2["output"] == "fetch result"

    # Messages pass through unchanged
    assert msgs is messages


def test_messages_to_turns_multi_tool_interleaved():
    """Multi-tool with interleaved response — tool → tool → response."""
    messages: list[BaseMessage] = [
        HumanMessage(content="A then B"),
        AIMessage(content="", tool_calls=[{"name": "A", "args": {}, "id": "a1"}]),
        ToolMessage(content="A done", tool_call_id="a1"),
        AIMessage(content="", tool_calls=[{"name": "B", "args": {}, "id": "b1"}]),
        ToolMessage(content="B done", tool_call_id="b1"),
        AIMessage(content="All done."),
    ]

    turns, _ = _messages_to_turns(messages)
    assert len(turns) == 1
    assert len(turns[0].tool_calls) == 2
    assert turns[0].tool_calls[0]["name"] == "A"
    assert turns[0].tool_calls[0]["output"] == "A done"
    assert turns[0].tool_calls[1]["name"] == "B"
    assert turns[0].tool_calls[1]["output"] == "B done"


def test_messages_to_turns_preserves_cron_turn_boundary():
    """Persisted cron batches keep a separate restored turn after a user turn."""
    messages: list[BaseMessage] = [
        HumanMessage(content="5s后提醒我吃饭"),
        AIMessage(
            content="",
            tool_calls=[{"name": "cron", "args": {"interval_seconds": 5}, "id": "u1"}],
        ),
        ToolMessage(content="Scheduled: 123", tool_call_id="u1"),
        AIMessage(content="好的，已设置5秒后提醒你吃饭。"),
        AIMessage(
            content="",
            additional_kwargs={"alex_turn_start": True, "alex_turn_kind": "cron"},
            tool_calls=[{"name": "cron", "args": {"job_id": "123"}, "id": "c1"}],
        ),
        ToolMessage(content="该吃饭啦！", tool_call_id="c1"),
        AIMessage(content="到点了，该吃饭啦！"),
    ]

    turns, _ = _messages_to_turns(messages)
    assert len(turns) == 2
    assert turns[0].kind == "user"
    assert turns[0].user_input == "5s后提醒我吃饭"
    assert turns[0].response == "好的，已设置5秒后提醒你吃饭。"
    assert turns[1].kind == "cron"
    assert turns[1].user_input == ""
    assert turns[1].response == "到点了，该吃饭啦！"
