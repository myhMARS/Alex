"""Regression tests for TUI rendering."""

import pytest
pytest.importorskip("textual")
from textual.app import App, ComposeResult

from alex.tui import AlexApp, AlexBubble, ChatTurn, ToolBubble


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
    def __init__(self, notes: list[dict]) -> None:
        self._notes = list(notes)

    def pop_notifications(self) -> list[dict]:
        notes = self._notes[:]
        self._notes.clear()
        return notes


@pytest.mark.asyncio
async def test_reflect_notification_shows_toast():
    agent = _AgentStub([{
        "type": "skill_reflect",
        "new": 1,
        "updated": 0,
        "deprecated": 0,
        "names": ["foo"],
    }])
    app = AlexApp(agent)

    async with app.run_test() as pilot:
        pilot.app._show_reflect_notification()
        await pilot.pause()
        toasts = list(pilot.app.query(".toast"))
        assert len(toasts) == 1
        assert "toast-hidden" not in toasts[0].classes
