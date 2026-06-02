"""Textual TUI for Alex agent — 直接通过 bus 与所有模块通信。"""

from __future__ import annotations

from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Header, Input, Static

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
    ToolsProvided,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from alex.kernel.contracts.chat import FeedbackSubmitted, UserTurnRequested
from alex.tui.view_models import ChatHistory
from alex.tui.view_state import SessionViewState
from alex.tui.chat_projector import ChatProjector
from alex.tui.notification_controller import NotificationController
from alex.tui.controller import ChatControllerMixin


class AlexApp(ChatControllerMixin, App):
    """Alex Agent TUI — 直接通过 bus 与所有模块通信，无 AgentFacade 中间层。"""

    TITLE = "Alex"
    SUB_TITLE = "/help for shortcuts"

    CSS_PATH = "alex.tcss"

    ENABLE_COMMAND_PALETTE = False
    SELECTION_ENABLED = True

    BINDINGS = [
        Binding("ctrl+t", "toggle_thinking", "Thinking", show=False, priority=True),
        Binding("ctrl+k", "toggle_skills", "Skills", show=False, priority=True),
        Binding("ctrl+o", "toggle_tool_output", "Tool Output", show=False, priority=True),
        Binding("ctrl+g", "rate_good", "Good", show=False, priority=True),
        Binding("ctrl+b", "rate_bad", "Bad", show=False, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, bus: AsyncEventBus | None = None, *, host_managed: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self._bus: AsyncEventBus = bus or AsyncEventBus()
        self._host_managed = host_managed
        self._history = ChatHistory()
        self._thinking_expanded = False
        self._skills_expanded = False
        self._tool_output_expanded = False

        self._view_state = SessionViewState()
        self._notif = NotificationController(self, self._view_state)
        self._projector = ChatProjector(self)
        self._mcp_status_message: str = "未开始加载"
        self._mcp_servers: list[dict] = []
        self._status_timer: Timer | None = None

    # Public contract properties — Package-private _-prefixed internals
    # exposed through these as a stable interface for collaborators.

    @property
    def chat_history(self) -> ChatHistory:
        return self._history

    @property
    def message_bus(self) -> Any:
        return self._bus

    @property
    def notif(self) -> NotificationController:
        return self._notif

    @property
    def tool_output_expanded(self) -> bool:
        return self._tool_output_expanded

    @property
    def view_state(self) -> SessionViewState:
        return self._view_state

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
        """Start fresh — subscribe to bus events."""
        self.query_one("#input-box", Input).focus()
        self._status_timer = self.set_interval(1.0, self._refresh_status_bar_tick)
        self._projector.refresh_status_bar()
        self._start_services_with_bus()

    def _refresh_status_bar_tick(self) -> None:
        self._projector.refresh_status_bar()

    @work(exclusive=True)
    async def _start_services_with_bus(self) -> None:
        bus = self._bus
        # Bus may already be started by ModuleHost — start() is idempotent
        await bus.start()

        # 绑定 NotificationController 到 bus（订阅权限请求事件）
        await self._notif.bind_bus(bus)

        # 订阅所有 UI 相关事件
        # 使用 _wrap_handler 确保 handler 在 Textual app context 中执行
        p = self._projector
        await bus.subscribe(CronJobEvent, self._wrap_handler(p.on_cron_job_event))
        await bus.subscribe(CronDebugEvent, self._wrap_handler(p.on_cron_debug_event))
        await bus.subscribe(SkillReflectEvent, self._wrap_handler(p.on_skill_reflect_event))
        await bus.subscribe(SkillReflectErrorEvent, self._wrap_handler(p.on_skill_reflect_error_event))
        await bus.subscribe(TurnStarted, self._wrap_handler(p.on_turn_started))
        await bus.subscribe(SkillLoaded, self._wrap_handler(p.on_skill_loaded))
        await bus.subscribe(ThinkingUpdated, self._wrap_handler(p.on_thinking))
        await bus.subscribe(TokenEmitted, self._wrap_handler(p.on_token))
        await bus.subscribe(ToolStarted, self._wrap_handler(p.on_tool_started))
        await bus.subscribe(ToolFinished, self._wrap_handler(p.on_tool_finished))
        await bus.subscribe(ToolsProvided, self._wrap_handler(self._on_mcp_tools_provided))
        await bus.subscribe(TurnCompleted, self._wrap_handler(p.on_turn_completed))
        await bus.subscribe(TurnFailed, self._wrap_handler(p.on_turn_failed))

    def _wrap_handler(self, handler):
        """包装 bus 事件 handler，确保在 Textual app context 中执行。

        bus dispatch loop 不在 Textual context 中，直接操作 widget 会
        触发 LookupError: active_app。用 run_worker 调度到 app context。
        """
        async def _wrapped(event):
            self.run_worker(handler(event), exclusive=False)
        return _wrapped

    async def _on_mcp_tools_provided(self, event: ToolsProvided) -> None:
        """Called when MCPModule announces tools or status — update status display."""
        if event.provider == "mcp":
            meta = event.metadata or {}
            msg = meta.get("message", "")
            if msg:
                self._mcp_status_message = msg
            elif event.specs:
                self._mcp_status_message = f"已连接，注册 {len(event.specs)} 个工具"
            servers = meta.get("servers")
            if isinstance(servers, list):
                self._mcp_servers = servers
            self._projector.refresh_status_bar()

    def action_quit(self) -> None:
        self._do_shutdown()

    @work(exclusive=True)
    async def _do_shutdown(self) -> None:
        self.exit()

    # ── key-binding actions ─────────────────────────────────────────────

    def action_rate_good(self) -> None:
        self._submit_feedback(positive=True)

    def action_rate_bad(self) -> None:
        self._submit_feedback(positive=False)

    def _submit_feedback(self, positive: bool) -> None:
        """Submit feedback via bus event."""
        vs = self._view_state
        if vs.last_response_rated:
            return
        vs.last_response_rated = True
        turn_id = vs.pending_feedback_turn_id
        vs.pending_feedback_turn_id = ""

        # 发布反馈事件到 bus → agent/skill 模块处理
        self._bus.publish(FeedbackSubmitted(
            session_id=self._history.session_id,
            turn_id=turn_id,
            positive=positive,
        ))

        if not positive:
            self._notif.show_toast("已标记为不满意，正在反思…", duration=2)
        self._notif.dismiss_feedback()

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

        # ── modal gate ──
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

        # 发布用户消息到 bus → agent 模块订阅并处理
        self._run_chat(user_input)

    @work
    async def _run_chat(self, user_input: str) -> None:
        """发布 UserTurnRequested 到 bus，agent 模块处理后通过事件流式返回结果。"""
        self._bus.publish(UserTurnRequested(
            session_id=self._history.session_id,
            user_text=user_input,
        ))
