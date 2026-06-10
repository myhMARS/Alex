"""Regression tests for TUI rendering and session lifecycle."""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
pytest.importorskip("textual")
from textual.app import App, ComposeResult
from textual.widgets import Input
from textual.widgets import Static

from alex import messages as msg
from alex.tui import (
    AlexApp,
    AlexBubble,
    ChatHistory,
    ChatTurn,
    ToolBubble,
    _messages_to_turns,
)
from alex.tui.app import _compute_mcp_status
from alex.tui.chat_projector import ChatProjector
from alex.store import session as session_store
from alex.store.session_adapter import SessionPersistence
from alex.mcp.mcp_client import MCPConnection
from alex.config import MCPServerConfig


def _tc(name: str, args: dict | None = None, tc_id: str = "") -> dict:
    """Build an OpenAI-format tool call dict."""
    import json
    return {
        "id": tc_id or f"call_{name}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args or {}),
        },
    }


class _BubbleHarness(App[None]):
    def __init__(self, bubble: AlexBubble) -> None:
        super().__init__()
        self._bubble = bubble

    def compose(self) -> ComposeResult:
        yield self._bubble


def _stored_renderable(static: Static):
    return getattr(static, "_Static__content")


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
        assert "response-prefix" in getattr(children[0], "classes", set())
        assert isinstance(children[1], ToolBubble)
        assert "response-text" in getattr(children[-1], "classes", set())


@pytest.mark.asyncio
async def test_tool_done_output_has_no_success_checkmark():
    bubble = AlexBubble()
    turn = ChatTurn(
        user_input="写入记忆",
        response="",
        tool_calls=[
            {
                "name": "memory-bear.write_memory",
                "args": {"content": "hello"},
                "output": '{"success":true,"msg_id":"0882696d-606b-43ba-91bc-1c4305b107e0"}',
            }
        ],
    )

    async with _BubbleHarness(bubble).run_test() as pilot:
        bubble.finalize(turn)
        await pilot.pause()

        tool = next(child for child in bubble.children if isinstance(child, ToolBubble))
        assert tool.border_title == "🔧 memory-bear.write_memory"

        output = tool.query_one("#tool-output-summary", Static)
        assert _stored_renderable(output) == '└─ {"success":true,"msg_id":"0882696d-606b-43ba-91bc-1c4305b107e0"}'


@pytest.mark.asyncio
async def test_finalize_coalesces_read_calls_into_single_file_list():
    bubble = AlexBubble()
    turn = ChatTurn(
        user_input="查看配置",
        response="",
        tool_calls=[
            {
                "name": "read",
                "args": {"path": "alex/config.py"},
                "output": "Path: C:\\repo\\alex\\config.py\nBytes: 123\n\nfile content",
            },
            {
                "name": "read",
                "args": {"path": "main.py"},
                "output": "Path: C:\\repo\\main.py\nBytes: 45\n\nother content",
            },
        ],
    )

    async with _BubbleHarness(bubble).run_test() as pilot:
        bubble.finalize(turn)
        await pilot.pause()

        tools = [child for child in bubble.children if isinstance(child, ToolBubble)]
        assert len(tools) == 1
        tool = tools[0]
        assert tool.border_title == "🔧 read (2 files)"
        assert not tool.query("#tool-args")
        summary = tool.query_one("#tool-output-summary", Static)
        full = tool.query_one("#tool-output-full", Static)
        assert _stored_renderable(summary) == "└─ Read 2 files\n   • repo/alex/config.py\n   • repo/main.py [Ctrl+O]"
        assert _stored_renderable(full) == "repo/alex/config.py\nrepo/main.py"


@pytest.mark.asyncio
async def test_tool_output_can_toggle_between_summary_and_full():
    bubble = AlexBubble()
    turn = ChatTurn(
        user_input="搜索定义",
        response="",
        tool_calls=[
            {
                "name": "grep",
                "args": {"pattern": "get_llm_config"},
                "output": "Pattern: /get_llm_config/\nFiles:\nconfig.py\n\nconfig.py:22:def get_llm_config()",
            }
        ],
    )

    async with _BubbleHarness(bubble).run_test() as pilot:
        bubble.finalize(turn)
        await pilot.pause()

        tool = next(child for child in bubble.children if isinstance(child, ToolBubble))
        args = tool.query_one("#tool-args", Static)
        summary = tool.query_one("#tool-output-summary", Static)
        full = tool.query_one("#tool-output-full", Static)
        summary_text = _stored_renderable(summary)
        assert "hidden" not in getattr(args, "classes", set())
        assert "hidden" not in getattr(summary, "classes", set())
        assert "hidden" in getattr(full, "classes", set())
        assert "└─ Pattern: /get_llm_config/" in summary_text
        assert "\n   Files:" in summary_text
        assert "\n   config.py" in summary_text
        assert "\n   ... (2 more lines) [Ctrl+O]" in summary_text

        bubble.set_tool_output_expanded(True)
        await pilot.pause()
        assert "hidden" not in getattr(args, "classes", set())
        assert "hidden" in getattr(summary, "classes", set())
        assert "hidden" not in getattr(full, "classes", set())
        assert _stored_renderable(full) == "Pattern: /get_llm_config/\nFiles:\nconfig.py\n\nconfig.py:22:def get_llm_config()"

        bubble.set_tool_output_expanded(False)
        await pilot.pause()
        assert "hidden" not in getattr(args, "classes", set())
        assert "hidden" not in getattr(summary, "classes", set())
        assert "hidden" in getattr(full, "classes", set())


@pytest.mark.asyncio
async def test_streaming_read_calls_share_single_bubble_and_path_list():
    from alex.tui.stream_renderer import StreamRenderer

    bubble = AlexBubble()
    async with _BubbleHarness(bubble).run_test() as pilot:
        renderer = StreamRenderer(bubble)
        renderer.on_tool_started("r1", "read", {"path": "alex/config.py"})
        renderer.on_tool_finished("r1", "Path: C:\\repo\\alex\\config.py\nBytes: 123\n\nfile content")
        renderer.on_tool_started("r2", "read", {"path": "main.py"})
        renderer.on_tool_finished("r2", "Path: C:\\repo\\main.py\nBytes: 45\n\nother content")
        await pilot.pause()

        tools = [child for child in bubble.children if isinstance(child, ToolBubble)]
        assert len(tools) == 1
        tool = tools[0]
        assert tool.border_title == "🔧 read (2 files)"
        assert not tool.query("#tool-args")
        summary = tool.query_one("#tool-output-summary", Static)
        full = tool.query_one("#tool-output-full", Static)
        assert _stored_renderable(summary) == "└─ Read 2 files\n   • repo/alex/config.py\n   • repo/main.py [Ctrl+O]"
        assert _stored_renderable(full) == "repo/alex/config.py\nrepo/main.py"


@pytest.mark.asyncio
async def test_streaming_thinking_updates_live_bubble():
    from alex.tui.stream_renderer import StreamRenderer

    bubble = AlexBubble()
    async with _BubbleHarness(bubble).run_test() as pilot:
        renderer = StreamRenderer(bubble)
        renderer.on_thinking("step 1")
        renderer.on_thinking(" -> step 2")
        await pilot.pause()

        collapsed = bubble.query_one(".thinking-collapsed", Static)
        expanded = bubble.query_one(".thinking-expanded", Static)
        response = bubble.query_one(".response-text", Static)

        assert _stored_renderable(collapsed) == "💭 Thinking (16 chars) [Ctrl+T]"
        assert _stored_renderable(expanded) == "step 1 -> step 2"
        assert "hidden" not in getattr(collapsed, "classes", set())
        assert "hidden" in getattr(expanded, "classes", set())
        assert _stored_renderable(response) == ""


class _AgentStub:
    """Legacy stub — no longer used by AlexApp but kept for reference."""
    def __init__(self, notes: list | None = None) -> None:
        self._notes = list(notes or [])
        self._history: list[dict] = []
        self._cron_history: list[dict] = []
        self._session_id: str = ""
        self._bus = None

    @property
    def bus(self):
        return self._bus

    def bind_event_bus(self, bus) -> None:
        self._bus = bus

    def bind_event_loop(self, loop) -> None:
        pass

    async def start_services(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def provide_feedback(self, positive: bool) -> None:
        pass

    def list_cron_jobs(self) -> list[dict]:
        return []

    def list_skills(self) -> list[dict]:
        return []

    def delete_skill(self, target: str) -> str | None:
        return None

    def deprecate_skill(self, target: str) -> str | None:
        return None

    async def merge_skills(self) -> dict:
        return {}

    async def reflect(self) -> None:
        pass

    @property
    def last_turn_result(self):
        return None

    async def chat_stream(self, user_input: str):
        yield None

    async def restore_history(self, messages: list) -> None:
        self._history = list(messages)

    async def clear_history(self) -> None:
        self._history.clear()

    def set_session_context(self, session_id: str, cron_history: list[dict] | None = None) -> None:
        self._session_id = session_id
        self._cron_history = list(cron_history or [])

    def list_session_cron_history(self, query: str = "", limit: int = 20) -> list[dict]:
        return list(self._cron_history)[:limit]

    def list_sessions(self) -> list[dict]:
        from alex.store.session_adapter import SessionPersistence
        return SessionPersistence.list_sessions()

    def load_session(self, session_id: str) -> dict | None:
        from alex.store.session_adapter import SessionPersistence
        return SessionPersistence.load(session_id)

    async def subscribe_store(self, bus) -> None:
        pass

    @property
    def history(self) -> list:
        return self._history


@pytest.mark.asyncio
async def test_reflect_notification_shows_toast():
    from alex.bus.events import SkillReflectEvent
    from alex.bus import AsyncEventBus

    bus = AsyncEventBus()
    await bus.start()
    agent = _AgentStub()
    app = AlexApp(bus)

    async with app.run_test() as pilot:
        await bus.subscribe(SkillReflectEvent, pilot.app._projector.on_skill_reflect_event)
        bus.publish(SkillReflectEvent(new=1, updated=0, deprecated=0, names=["foo"]))
        await pilot.pause()
        toasts = list(pilot.app.query(".alex-toast"))
        assert len(toasts) >= 1
        visible = [t for t in toasts if "alex-toast-hidden" not in getattr(t, "classes", set())]
        assert len(visible) >= 1

    await bus.shutdown()


@pytest.mark.asyncio
async def test_ctrl_o_toggles_tool_output_and_help_mentions_shortcut():
    agent = _AgentStub()
    app = AlexApp()
    css_path = Path(__file__).parent.parent / "alex" / "tui" / "alex.tcss"
    assert "ToolBubble > .hidden" in css_path.read_text()

    async with app.run_test() as pilot:
        app._show_help()
        await pilot.pause()
        page = app.query_one("#page-content", Static)
        assert "Ctrl+O" in str(_stored_renderable(page))
        assert "/output" in str(_stored_renderable(page))

        bubble = AlexBubble()
        await app.query_one("#chat-view").mount(bubble)
        await pilot.pause()
        tool = bubble.insert_tool("grep", {"pattern": "get_llm_config"})
        tool.set_done("line1\nline2")
        await pilot.pause()

        args = tool.query_one("#tool-args", Static)
        summary = tool.query_one("#tool-output-summary", Static)
        full = tool.query_one("#tool-output-full", Static)
        assert _stored_renderable(summary) == "└─ line1\n   line2 [Ctrl+O]"
        assert "hidden" not in getattr(args, "classes", set())
        assert "hidden" not in getattr(summary, "classes", set())
        assert "hidden" in getattr(full, "classes", set())

        app.action_toggle_tool_output()
        await pilot.pause()
        assert "hidden" not in getattr(args, "classes", set())
        assert "hidden" in getattr(summary, "classes", set())
        assert "hidden" not in getattr(full, "classes", set())


@pytest.mark.asyncio
async def test_output_command_toggles_tool_output():
    agent = _AgentStub()
    app = AlexApp()

    async with app.run_test() as pilot:
        bubble = AlexBubble()
        await app.query_one("#chat-view").mount(bubble)
        await pilot.pause()
        tool = bubble.insert_tool("grep", {"pattern": "get_llm_config"})
        tool.set_done("line1\nline2")
        await pilot.pause()

        args = tool.query_one("#tool-args", Static)
        summary = tool.query_one("#tool-output-summary", Static)
        full = tool.query_one("#tool-output-full", Static)
        assert _stored_renderable(summary) == "└─ line1\n   line2 [Ctrl+O]"
        assert "hidden" not in getattr(args, "classes", set())
        assert "hidden" not in getattr(summary, "classes", set())
        assert "hidden" in getattr(full, "classes", set())

        input_widget = app.query_one("#input-box", Input)
        input_widget.value = "/output"
        app.on_input_submitted(Input.Submitted(input_widget, "/output", validation_result=None))
        await pilot.pause()

        assert "hidden" not in getattr(args, "classes", set())
        assert "hidden" in getattr(summary, "classes", set())
        assert "hidden" not in getattr(full, "classes", set())


def test_reflect_event_shows_updated_skill_names():
    from alex.bus.events import SkillReflectEvent

    evt = SkillReflectEvent(updated=1, updated_names=["Timer Reminder"])
    assert evt.toast == "Skills refined — 1 updated: Timer Reminder"


# ── session lifecyle regression tests ─────────────────────────────────────

@pytest.mark.asyncio
async def test_input_widget_id_correct():
    """Regression: #input-box exists; the wrong #user-input would crash."""
    app = AlexApp()

    async with app.run_test() as pilot:
        input_widget = pilot.app.query_one("#input-box", Input)
        assert input_widget is not None

        from textual.css.query import NoMatches
        with pytest.raises(NoMatches):
            pilot.app.query_one("#user-input", Input)


@pytest.mark.asyncio
async def test_status_bar_shows_cron_jobs_immediately_after_startup():
    from alex.bus import AsyncEventBus
    from alex.bus.events import CronJobEvent

    bus = AsyncEventBus()
    await bus.start()

    app = AlexApp(bus)

    async with app.run_test() as pilot:
        # 通过 bus 事件注入 cron job 数据
        bus.publish(CronJobEvent(
            job_id="job-1",
            name="日报提醒",
            status="SCHEDULED",
        ))
        await pilot.pause()
        await asyncio.sleep(0.05)
        pilot.app._refresh_status_bar_tick()
        await pilot.pause()
        status_widget = pilot.app.query_one("#status-content")
        status = getattr(status_widget, "_Static__content")
        assert "日报提醒" in str(status)
        assert "SCHEDULED" in str(status)

    await bus.shutdown()


@pytest.mark.asyncio
async def test_status_bar_countdown_refreshes_with_time(monkeypatch):
    from alex.bus import AsyncEventBus
    from alex.bus.events import CronJobEvent

    bus = AsyncEventBus()
    await bus.start()

    now = {"value": 100.0}
    monkeypatch.setattr(time, "time", lambda: now["value"])

    app = AlexApp(bus)

    async with app.run_test() as pilot:
        # 通过 bus 事件注入 cron job 数据（带 next_run_at）
        bus.publish(CronJobEvent(
            job_id="job-1",
            name="日报提醒",
            status="SCHEDULED",
        ))
        await pilot.pause()
        await asyncio.sleep(0.05)
        # 手动设置 next_run_at（CronJobEvent 没有此字段，直接操作缓存）
        pilot.app._projector._cached_cron_jobs = [{
            "id": "job-1",
            "name": "日报提醒",
            "status": "SCHEDULED",
            "next_run_at": 105.0,
            "runs_done": 0,
        }]
        pilot.app._refresh_status_bar_tick()
        await pilot.pause()
        status_widget = pilot.app.query_one("#status-content")
        first = str(getattr(status_widget, "_Static__content"))
        assert "next:5s" in first

        now["value"] = 102.0
        pilot.app._refresh_status_bar_tick()
        await pilot.pause()

    await bus.shutdown()


@pytest.mark.asyncio
async def test_mcp_command_shows_runtime_status():
    agent = _AgentStub([])
    app = AlexApp()
    app._connect_mcp = AsyncMock()
    app._mcp_status_message = "已处理 3 个 server，连接成功 1 个，注册 2 个工具"

    class _Pool:
        def __init__(self):
            self.connections = [
                MCPConnection(
                    config=MCPServerConfig(
                        name="local-server",
                        transport="stdio",
                        command="your-mcp-command",
                    ),
                    tools=["t1", "t2"],
                ),
                MCPConnection(
                    config=MCPServerConfig(
                        name="http-server",
                        transport="streamable-http",
                        url="http://localhost:8000/mcp",
                    ),
                    error="RuntimeError: boom",
                ),
                MCPConnection(
                    config=MCPServerConfig(
                        name="disabled-server",
                        transport="sse",
                        url="http://localhost:8123/sse",
                        enabled=False,
                    ),
                    error="disabled",
                ),
            ]

    app._mcp_pool = _Pool()

    async with app.run_test() as pilot:
        pilot.app._handle_mcp_cmd()
        await pilot.pause()

        assert pilot.app._view_state.page_mode == "mcp"
        title = str(getattr(pilot.app.query_one("#page-title"), "_Static__content"))
        assert "MCP 状态" in title

        from textual.widgets import Tree
        tree = pilot.app.query_one(Tree)
        labels: list[str] = []
        for node in tree.root.children:
            labels.append(str(node.label.plain if hasattr(node.label, "plain") else node.label))
        tree_text = " ".join(labels)
        assert "local-server" in tree_text
        assert "http-server" in tree_text
        assert "disabled-server" in tree_text


@pytest.mark.asyncio
async def test_mcp_command_shows_global_failure_without_pool():
    agent = _AgentStub([])
    app = AlexApp()
    app._connect_mcp = AsyncMock()
    app._mcp_status_message = "加载失败：ValueError: bad config"

    async with app.run_test() as pilot:
        pilot.app._handle_mcp_cmd()
        await pilot.pause()

        title = str(getattr(pilot.app.query_one("#page-title"), "_Static__content"))
        assert "加载失败：ValueError: bad config" in title


@pytest.mark.asyncio
async def test_mcp_command_shows_expandable_tool_details():
    app = AlexApp()
    app._connect_mcp = AsyncMock()
    app._mcp_servers = [{
        "name": "memory-server",
        "transport": "streamable-http",
        "url": "http://localhost:8000/mcp",
        "enabled": True,
        "status": "CONNECTED",
        "tool_count": 1,
        "error": None,
        "tools": [{
            "name": "mcp__memory__search",
            "description": "Search stored memories",
            "json_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }],
    }]
    app._mcp_status_message = _compute_mcp_status(app._mcp_servers)

    async with app.run_test() as pilot:
        pilot.app._handle_mcp_cmd()
        await pilot.pause()

        title = str(getattr(pilot.app.query_one("#page-title"), "_Static__content"))
        assert "台" not in title

        from textual.widgets import Tree
        tree = pilot.app.query_one(Tree)
        server_node = tree.root.children[0]
        tool_node = server_node.children[0]
        labels = [str(tool_node.label.plain if hasattr(tool_node.label, "plain") else tool_node.label)]
        labels.extend(
            str(child.label.plain if hasattr(child.label, "plain") else child.label)
            for child in tool_node.children
        )
        schema_node = tool_node.children[1]
        labels.extend(
            str(child.label.plain if hasattr(child.label, "plain") else child.label)
            for child in schema_node.children
        )

        detail_text = " ".join(labels)
        assert "mcp__memory__search" in detail_text
        assert "描述: Search stored memories" in detail_text
        assert "输入参数" in detail_text
        assert '"query"' in detail_text
        assert '"required"' in detail_text


@pytest.mark.asyncio
async def test_resume_restores_agent_memory():
    """Session resume sends ReplaceMemory request via bus."""
    from alex.bus import AsyncEventBus
    from alex.memory.module import MemoryModule
    from alex.kernel.contracts.memory import GetContext
    from alex.kernel.contracts.session import ListSessions, LoadSession

    session_id = "test_resume_memory"
    messages: list[dict] = [
        msg.user_message("Hello"),
        msg.assistant_message("", tool_calls=[_tc("time", tc_id="c1")]),
        msg.tool_message("10:00 UTC", tool_call_id="c1"),
        msg.assistant_message("It is 10 AM.", reasoning_content="got time"),
    ]
    session_store.save_session(session_id, messages)

    bus = AsyncEventBus()
    await bus.start()
    mem = MemoryModule()
    await mem.start(bus)

    # Provide session handlers
    async def _list_sessions(req):
        return SessionPersistence.list_sessions()
    async def _load_session(req):
        from alex.store.session import load_session_bundle
        return load_session_bundle(req.session_id)
    bus.provide(ListSessions, _list_sessions)
    bus.provide(LoadSession, _load_session)

    app = AlexApp(bus)

    async with app.run_test() as pilot:
        worker = app._resume_session(session_id)
        for _ in range(50):
            await pilot.pause(0.05)
            if worker.is_finished:
                break

        assert worker.is_finished
        # Verify memory was restored via bus
        ctx = await bus.request(GetContext(session_id=session_id))
        assert len(ctx) == len(messages)
        assert ctx[0]["content"] == "Hello"
        assert ctx[3]["content"] == "It is 10 AM."

        input_widget = pilot.app.query_one("#input-box", Input)
        assert not input_widget.disabled

    await bus.shutdown()


@pytest.mark.asyncio
async def test_clear_clears_agent_memory():
    """Session clear sends ClearMemory request via bus."""
    from alex.bus import AsyncEventBus
    from alex.memory.module import MemoryModule
    from alex.kernel.contracts.memory import GetContext
    from alex.kernel.contracts.session import ListSessions, LoadSession

    session_id = "test_clear_memory"
    session_store.save_session(session_id, [
        msg.user_message("Hi"),
        msg.assistant_message("Hey!"),
    ])

    bus = AsyncEventBus()
    await bus.start()
    mem = MemoryModule()
    await mem.start(bus)

    async def _list_sessions(req):
        return SessionPersistence.list_sessions()
    async def _load_session(req):
        from alex.store.session import load_session_bundle
        return load_session_bundle(req.session_id)
    bus.provide(ListSessions, _list_sessions)
    bus.provide(LoadSession, _load_session)

    app = AlexApp(bus)

    async with app.run_test() as pilot:
        w_load = app._resume_session(session_id)
        for _ in range(50):
            await pilot.pause(0.05)
            if w_load.is_finished:
                break

        ctx = await bus.request(GetContext(session_id=session_id))
        assert len(ctx) > 0

        w_clear = app._clear_chat()
        for _ in range(50):
            await pilot.pause(0.05)
            if w_clear.is_finished:
                break

        assert w_clear.is_finished
        ctx = await bus.request(GetContext(session_id=session_id))
        assert len(ctx) == 0

        input_widget = pilot.app.query_one("#input-box", Input)
        assert not input_widget.disabled

    await bus.shutdown()


@pytest.mark.asyncio
async def test_chat_allows_multiple_submissions_to_enqueue():
    """Multiple user submissions publish UserTurnRequested events to bus."""
    from alex.bus import AsyncEventBus
    from alex.kernel.contracts.chat import UserTurnRequested

    bus = AsyncEventBus()
    await bus.start()

    received: list[UserTurnRequested] = []

    async def _handler(event):
        received.append(event)

    await bus.subscribe(UserTurnRequested, _handler)

    app = AlexApp(bus)

    async with app.run_test() as pilot:
        for _ in range(50):
            await pilot.pause(0.05)
            if pilot.app._bus._running:
                break

        input_widget = pilot.app.query_one("#input-box", Input)
        input_widget.value = "hello"
        app.on_input_submitted(Input.Submitted(input_widget, "hello", validation_result=None))
        await pilot.pause()

        input_widget.value = "world"
        app.on_input_submitted(Input.Submitted(input_widget, "world", validation_result=None))
        await pilot.pause()

        # 等待 bus 事件分发
        await asyncio.sleep(0.1)

        # 验证两条 UserTurnRequested 都发布到了 bus
        assert len(received) == 2
        assert received[0].user_text == "hello"
        assert received[1].user_text == "world"

        assert not input_widget.disabled

    await bus.shutdown()


# ── ChatHistory message-sequence fidelity ──────────────────────────────────

def test_chat_history_preserves_loaded_messages():
    """Loaded messages survive save/load round-trip without degradation."""
    session_id = "test_fidelity"
    original: list[dict] = [
        msg.user_message("What time is it?"),
        msg.assistant_message("", tool_calls=[_tc("time", {"tz": "UTC"}, tc_id="call_1")]),
        msg.tool_message("2026-05-20 10:00:00", tool_call_id="call_1"),
        msg.assistant_message("It is 10 AM UTC.", reasoning_content="check"),
    ]
    session_store.save_session(session_id, original)

    h1 = ChatHistory(session_id=session_id)
    bundle = SessionPersistence.load(session_id)
    assert bundle is not None
    h1.restore_from_bundle(bundle)
    new_delta: list[dict] = [
        msg.user_message("Thanks"),
        msg.assistant_message("You're welcome!"),
    ]
    h1.add(ChatTurn(user_input="Thanks", response="You're welcome!"), messages_delta=new_delta)
    SessionPersistence.save(session_id, h1.loaded_messages)

    h2 = ChatHistory(session_id=session_id)
    bundle2 = SessionPersistence.load(session_id)
    assert bundle2 is not None
    h2.restore_from_bundle(bundle2)

    assert len(h2.turns) == 2
    assert h2.turns[0].user_input == "What time is it?"
    assert h2.turns[0].response == "It is 10 AM UTC."
    assert h2.turns[0].thinking == "check"
    assert len(h2.turns[0].tool_calls) == 1
    assert h2.turns[0].tool_calls[0]["name"] == "time"

    msgs = h2.loaded_messages
    assert len(msgs) == len(original) + len(new_delta)
    assert msgs[0]["content"] == "What time is it?"
    assert msgs[3]["content"] == "It is 10 AM UTC."
    assert msgs[4]["content"] == "Thanks"
    assert msgs[5]["content"] == "You're welcome!"


def test_chat_history_empty_session_is_valid():
    """An empty message list (after clear) is a valid loadable session."""
    session_id = "test_empty"
    session_store.save_session(session_id, [])

    h = ChatHistory(session_id=session_id)
    bundle = SessionPersistence.load(session_id)
    assert bundle is not None
    h.restore_from_bundle(bundle)
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
    bundle = SessionPersistence.load(session_id)
    assert bundle is not None
    h.restore_from_bundle(bundle)
    assert len(h.cron_history) == 1
    assert h.cron_history[0]["name"] == "提醒"


# ── _messages_to_turns multi-tool correctness ──────────────────────────────

def test_messages_to_turns_multi_tool():
    """A single AIMessage with multiple tool_calls must pair correctly."""
    messages: list[dict] = [
        msg.user_message("Search and fetch"),
        msg.assistant_message("", tool_calls=[
            _tc("search", {"q": "X"}, tc_id="t1"),
            _tc("fetch", {"url": "Y"}, tc_id="t2"),
        ]),
        msg.tool_message("search results", tool_call_id="t1"),
        msg.tool_message("fetch result", tool_call_id="t2"),
        msg.assistant_message("Done."),
    ]

    turns, msgs = _messages_to_turns(messages)
    assert len(turns) == 1
    turn = turns[0]
    assert turn.user_input == "Search and fetch"
    assert turn.response == "Done."
    assert len(turn.tool_calls) == 2

    t1 = turn.tool_calls[0]
    assert t1["name"] == "search"
    assert t1["output"] == "search results"

    t2 = turn.tool_calls[1]
    assert t2["name"] == "fetch"
    assert t2["output"] == "fetch result"

    assert msgs is messages


def test_messages_to_turns_multi_tool_interleaved():
    """Multi-tool with interleaved response — tool → tool → response."""
    messages: list[dict] = [
        msg.user_message("A then B"),
        msg.assistant_message("", tool_calls=[_tc("A", tc_id="a1")]),
        msg.tool_message("A done", tool_call_id="a1"),
        msg.assistant_message("", tool_calls=[_tc("B", tc_id="b1")]),
        msg.tool_message("B done", tool_call_id="b1"),
        msg.assistant_message("All done."),
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
    messages: list[dict] = [
        msg.user_message("5s后提醒我吃饭"),
        msg.assistant_message("", tool_calls=[_tc("cron", {"cron": "*/5 * * * *", "prompt": "提醒我吃饭"}, tc_id="u1")]),
        msg.tool_message("Scheduled: 123", tool_call_id="u1"),
        msg.assistant_message("好的，已设置5秒后提醒你吃饭。"),
        # Cron turn — marked with alex_turn_start
        {**msg.assistant_message("", tool_calls=[_tc("cron", {"job_id": "123"}, tc_id="c1")]),
         "alex_turn_start": True, "alex_turn_kind": "cron"},
        msg.tool_message("该吃饭啦！", tool_call_id="c1"),
        msg.assistant_message("到点了，该吃饭啦！"),
    ]

    turns, _ = _messages_to_turns(messages)
    assert len(turns) == 2
    assert turns[0].kind == "user"
    assert turns[0].user_input == "5s后提醒我吃饭"
    assert turns[0].response == "好的，已设置5秒后提醒你吃饭。"
    assert turns[1].kind == "cron"
    assert turns[1].user_input == ""
    assert turns[1].response == "到点了，该吃饭啦！"


def test_format_cron_jobs_page_shows_job_metadata():
    content = ChatProjector.format_cron_jobs_page([{
        "id": "job-1",
        "name": "日报提醒",
        "status": "SCHEDULED",
        "cron": "0 9 * * 1-5",
        "recurring": True,
        "durable": True,
        "next_run_at": 1760000000.0,
        "last_started_at": None,
        "last_finished_at": None,
        "prompt": "每天早上推送日报摘要",
        "last_result": "",
        "last_error": "",
    }])

    assert "当前 cron 任务 (1)" in content
    assert "[job-1] 日报提醒 (SCHEDULED)" in content
    assert "durable: True" in content
    assert "prompt: 每天早上推送日报摘要" in content
