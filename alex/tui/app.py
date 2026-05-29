"""Textual TUI for Alex agent — alternate screen with scrollable chat."""

from __future__ import annotations

import asyncio

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Header, Input, Static

from alex.agent.ports import AgentFacade
from alex.bus import AsyncEventBus
from alex.bus.events import (
    CronDebugEvent,
    CronJobEvent,
    SkillLoaded,
    SkillReflectErrorEvent,
    SkillReflectEvent,
    ThinkingUpdated,
    TokenEmitted,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from alex.tools.mcp_client import (
    MCPClientPool,
    MCPUnavailableError,
    load_mcp_tools_from_config,
)
from alex.tui.view_models import ChatHistory
from alex.tui.view_state import SessionViewState
from alex.tui.chat_projector import ChatProjector
from alex.tui.notification_controller import NotificationController
from alex.tui.controller import ChatControllerMixin


class AlexApp(ChatControllerMixin, App):
    """Alex Agent TUI — chat interface with scrollable history."""

    TITLE = "Alex"
    SUB_TITLE = "/help for shortcuts"

    CSS_PATH = "alex.tcss"

    BINDINGS = [
        Binding("ctrl+t", "toggle_thinking", "Thinking", show=False, priority=True),
        Binding("ctrl+k", "toggle_skills", "Skills", show=False, priority=True),
        Binding("ctrl+o", "toggle_tool_output", "Tool Output", show=False, priority=True),
        Binding("ctrl+g", "rate_good", "Good", show=False, priority=True),
        Binding("ctrl+b", "rate_bad", "Bad", show=False, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, agent: AgentFacade, event_bus: AsyncEventBus | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._agent = agent
        self._bus = event_bus or AsyncEventBus()
        self._history = ChatHistory()
        self._thinking_expanded = False
        self._skills_expanded = False
        self._tool_output_expanded = False

        # Phase 3: view state, projector, notifications replace scattered attrs
        self._view_state = SessionViewState()
        self._notif = NotificationController(self, self._view_state)
        self._projector = ChatProjector(self)
        self._mcp_pool: MCPClientPool | None = None
        self._mcp_status_message: str = "未开始加载"
        self._status_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left-pane"):
                yield VerticalScroll(id="chat-view")
                with VerticalScroll(id="page-view", classes="hidden"):
                    yield Static("", id="page-title")
                    yield Static("", id="page-content")
            with VerticalScroll(id="status-bar"):
                yield Static("后台任务", id="status-title")
                yield Static("", id="status-content")
        yield Input(placeholder="Message Alex... (Ctrl+G/B to rate, Ctrl+C: quit)", id="input-box")

    def on_mount(self) -> None:
        """Start fresh — no auto-restore."""
        self.query_one("#input-box", Input).focus()
        try:
            self._agent.bind_event_loop(asyncio.get_running_loop())
        except Exception:
            pass
        self._agent.set_session_context(self._history.session_id, self._history.cron_history)
        self._install_permission_hook()
        self._status_timer = self.set_interval(1.0, self._refresh_status_bar_tick)
        self._start_services_with_bus()

    def _refresh_status_bar_tick(self) -> None:
        self._projector.refresh_status_bar()

    def _install_permission_hook(self) -> None:
        """Inject the TUI confirm prompt into the agent's permission policy.

        Tolerant of stub agents (used in tests) that don't expose a
        ``permissions`` property.
        """
        existing = getattr(self._agent, "permissions", None)
        if existing is None:
            return
        existing.confirm_hook = self._notif.confirm_permission
        # Re-apply so any already-registered tools and the executor see
        # the updated hook (set_permissions also rewraps gated tools).
        try:
            self._agent.set_permissions(existing)
        except AttributeError:
            pass

    @work(exclusive=True)
    async def _start_services_with_bus(self) -> None:
        bus = self._bus
        if bus is not None:
            await bus.start()
            p = self._projector
            await bus.subscribe(CronJobEvent, p.on_cron_job_event)
            await bus.subscribe(CronDebugEvent, p.on_cron_debug_event)
            await bus.subscribe(SkillReflectEvent, p.on_skill_reflect_event)
            await bus.subscribe(SkillReflectErrorEvent, p.on_skill_reflect_error_event)
            await bus.subscribe(TurnStarted, p.on_turn_started)
            await bus.subscribe(SkillLoaded, p.on_skill_loaded)
            await bus.subscribe(ThinkingUpdated, p.on_thinking)
            await bus.subscribe(TokenEmitted, p.on_token)
            await bus.subscribe(ToolStarted, p.on_tool_started)
            await bus.subscribe(ToolFinished, p.on_tool_finished)
            await bus.subscribe(TurnCompleted, p.on_turn_completed)
            await bus.subscribe(TurnFailed, p.on_turn_failed)
            await self._agent.subscribe_store(bus)
            if self._agent.bus is None:
                self._agent.bind_event_bus(bus)
        try:
            await self._agent.start_services()
        except Exception:
            pass
        self._projector.refresh_status_bar()

        await self._connect_mcp()
        self._projector.refresh_status_bar()

    async def _connect_mcp(self) -> None:
        """Load and register MCP tools from ``~/.alex/mcp.json``.

        Failures are surfaced as toasts so a missing required dependency
        or a misconfigured server doesn't block startup.
        """
        self._mcp_status_message = "加载中"
        try:
            pool, tools = await load_mcp_tools_from_config()
        except MCPUnavailableError as e:
            self._mcp_status_message = f"不可用：{e}"
            self._notif.show_toast(f"MCP 不可用：{e}", duration=4)
            return
        except Exception as e:
            self._mcp_status_message = f"加载失败：{type(e).__name__}: {e}"
            self._notif.show_toast(f"MCP 加载失败：{type(e).__name__}: {e}", duration=4)
            return

        self._mcp_pool = pool
        connections = pool.connections
        connected = sum(1 for conn in connections if not conn.error)
        if not connections:
            self._mcp_status_message = "未发现 MCP server 配置"
        else:
            self._mcp_status_message = (
                f"已处理 {len(connections)} 个 server，连接成功 {connected} 个，注册 {len(tools)} 个工具"
            )
        if not tools:
            return
        for tool in tools:
            try:
                self._agent.register_tool(tool)
            except Exception:
                continue
        self._notif.show_toast(f"已加载 {len(tools)} 个 MCP 工具", duration=2)

    def action_quit(self) -> None:
        self._do_shutdown()

    @work(exclusive=True)
    async def _do_shutdown(self) -> None:
        try:
            await self._agent.shutdown()
        except Exception:
            pass
        if self._mcp_pool is not None:
            try:
                await self._mcp_pool.aclose()
            except Exception:
                pass
            self._mcp_pool = None
        self.exit()

    # ── key-binding actions delegated to NotificationController ─────────

    def action_rate_good(self) -> None:
        self._notif.rate_response(
            True, self._agent, self._view_state.pending_feedback_turn_id
        )

    def action_rate_bad(self) -> None:
        self._notif.rate_response(
            False, self._agent, self._view_state.pending_feedback_turn_id
        )

    # ── input handling ──────────────────────────────────────────────────

    @on(Input.Submitted, "#input-box")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user input submission."""
        user_input = event.value.strip()
        notif = self._notif

        if not user_input:
            return

        event.input.clear()

        cmd = user_input.lower()
        vs = self._view_state

        # ── modal gate: when a page panel is showing, only allow :q, /x, and resume selection ──
        if vs.page_mode is not None:
            if cmd == ":q":
                self._dismiss_overlay()
                return
            if cmd == "/x":
                notif.dismiss_toast()
                return
            if vs.showing_session_list and user_input.isdigit():
                self._handle_session_selection(user_input)
                return
            notif.show_toast("当前在面板页，输入 :q 返回对话", duration=2)
            return

        if cmd == ":q":
            self._dismiss_overlay()
            return

        if cmd == "/x":
            notif.dismiss_toast()
            return

        if cmd in ("/quit", "quit", "exit"):
            self.action_quit()
            return

        if cmd in ("/clear", "clear"):
            self._clear_chat()
            return

        if cmd == "/resume":
            self._show_session_list()
            return

        if cmd == "/merge-skills":
            self._run_merge_skills()
            return

        if cmd == "/reflect":
            self._run_force_reflection()
            return

        if cmd == "/help":
            self._show_help()
            return

        if cmd == "/output":
            self.action_toggle_tool_output()
            return

        if cmd == "/cron" or cmd.startswith("/cron "):
            self._handle_cron_cmd(user_input[5:].strip() if len(user_input) > 5 else "")
            return

        if cmd == "/mcp":
            self._handle_mcp_cmd()
            return

        if cmd == "/skills" or cmd.startswith("/skills "):
            self._handle_skills_cmd(user_input[7:].strip() if len(user_input) > 7 else "")
            return

        if user_input.startswith(("/", ":")):
            notif.show_toast(f"未知命令: {user_input}", duration=2)
            return

        # If previous response wasn't rated, treat the new message as implicit skip
        notif.dismiss_feedback()
        self._projector.note_user_submission(user_input)

        # Start async response; the user bubble is mounted only when the turn
        # is actually dequeued for processing.
        self._run_chat(user_input)

    @work
    async def _run_chat(self, user_input: str) -> None:
        """Run agent chat — rendering and history updates are bus-driven."""
        try:
            async for _ in self._agent.chat_stream(user_input):
                await asyncio.sleep(0)
        except Exception as e:
            self._notif.show_toast(f"对话执行失败：{e}", duration=3)
