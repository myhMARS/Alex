"""Textual TUI for Alex agent — alternate screen with scrollable chat."""

from __future__ import annotations

import asyncio
import time

from langchain_core.messages import BaseMessage

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
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
    ToolStarted,
    ToolFinished,
    CronBatch,
    CronDone,
    CronError,
)
from alex.tui.view_models import ChatHistory, ChatTurn
from alex.tui.presenter import AlexBubble, UserBubble
from alex.tui.stream_renderer import StreamRenderer
from alex.tui.controller import ChatControllerMixin


class AlexApp(ChatControllerMixin, App):
    """Alex Agent TUI — chat interface with scrollable history."""

    TITLE = "Alex"
    SUB_TITLE = "/help for shortcuts"

    CSS = """
    #main {
        height: 1fr;
    }
    #left-pane {
        height: 1fr;
        width: 1fr;
    }
    #chat-view {
        height: 1fr;
        overflow-y: scroll;
        padding: 0 1;
    }
    #page-view {
        height: 1fr;
        overflow-y: scroll;
        padding: 1 2;
    }
    #page-view.hidden {
        display: none;
    }
    #chat-view.hidden {
        display: none;
    }
    #page-title {
        text-style: bold;
        margin: 0 0 1 0;
        height: auto;
    }
    #page-content {
        height: auto;
    }
    #status-bar {
        width: 34;
        border: round $panel;
        border-title-color: $text-muted;
        padding: 0 1;
    }
    #status-title {
        text-style: bold;
        margin: 0 0 1 0;
        height: auto;
    }
    #status-content {
        height: 1fr;
    }
    #input-box {
        dock: bottom;
        height: 3;
        margin: 0 1;
    }
    .feedback-prompt {
        color: $text-muted;
        padding: 0 1;
        margin: 0;
        height: auto;
    }
    .feedback-done {
        color: $success;
        padding: 0 1;
        margin: 0;
        height: auto;
    }
    .toast {
        dock: top;
        height: 1;
        background: $panel;
        color: $text;
        padding: 0 2;
        text-align: right;
        text-style: bold;
    }
    .toast-hidden {
        display: none;
    }
    AlexBubble {
        margin: 0 0 0 0;
        padding: 0 1;
        border: round $success;
        border-title-color: $success;
        border-title-style: bold;
        height: auto;
    }
    AlexBubble > .thinking-collapsed {
        height: 1;
        color: $text-muted;
        padding: 0;
        margin: 0;
    }
    AlexBubble > .thinking-expanded {
        color: $text-muted;
        padding: 0 1;
        margin: 0 0 1 0;
        border: dashed $warning;
        border-title-color: $warning;
        height: auto;
    }
    AlexBubble > .skills-collapsed {
        height: 1;
        color: $text-muted;
        padding: 0;
        margin: 0;
    }
    AlexBubble > .skills-expanded {
        color: $text-muted;
        padding: 0;
        margin: 0 0 1 0;
        height: auto;
    }
    AlexBubble > .response-text {
        padding: 0;
        margin: 0;
        height: auto;
    }
    AlexBubble > .hidden {
        display: none;
    }
    ToolBubble {
        margin: 0 0 0 2;
        border: solid $primary;
        border-title-color: $text-muted;
        height: auto;
        padding: 0 1;
    }
    ToolBubble > #tool-args {
        color: $text-muted;
        padding: 0;
        margin: 0;
        height: auto;
    }
    ToolBubble > #tool-output {
        color: $success;
        padding: 0;
        margin: 0;
        height: auto;
    }
    """

    BINDINGS = [
        Binding("ctrl+t", "toggle_thinking", "Thinking", show=False, priority=True),
        Binding("ctrl+k", "toggle_skills", "Skills", show=False, priority=True),
        Binding("ctrl+g", "rate_good", "Good", show=False, priority=True),
        Binding("ctrl+b", "rate_bad", "Bad", show=False, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, agent: AgentFacade, event_bus: AsyncEventBus | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._agent = agent
        self._bus = event_bus
        self._history = ChatHistory()  # New session by default
        self._thinking_expanded = False
        self._skills_expanded = False
        self._showing_session_list = False
        self._page_mode: str | None = None
        self._last_response_rated = True  # start as rated (no pending feedback)
        self._pending_feedback_turn_id: str = ""  # turn_id for skill feedback binding
        self._feedback_widget: Static | None = None
        self._toast_widget: Static | None = None
        self._toast_timer: object = None
        self._cron_renderers: dict[str, StreamRenderer] = {}

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
        self._start_services_with_bus()

    @work(exclusive=True)
    async def _start_services_with_bus(self) -> None:
        bus = self._bus
        if bus is not None:
            await bus.start()
            await bus.subscribe(CronJobEvent, self._on_cron_job_event)
            await bus.subscribe(CronDebugEvent, self._on_cron_debug_event)
            await bus.subscribe(SkillReflectEvent, self._on_skill_reflect_event)
            await bus.subscribe(SkillReflectErrorEvent, self._on_skill_reflect_error_event)
            await bus.subscribe(ToolStarted, self._on_cron_tool_started)
            await bus.subscribe(ToolFinished, self._on_cron_tool_finished)
            await bus.subscribe(ThinkingUpdated, self._on_cron_thinking)
            await bus.subscribe(TokenEmitted, self._on_cron_token)
            await bus.subscribe(CronBatch, self._on_cron_batch)
            await bus.subscribe(CronDone, self._on_cron_done)
            await bus.subscribe(CronError, self._on_cron_error)
            await self._agent.subscribe_store(bus)
            if self._agent.bus is None:
                self._agent.bind_event_bus(bus)
        try:
            await self._agent.start_services()
        except Exception:
            pass

    def action_quit(self) -> None:
        self._do_shutdown()

    @work(exclusive=True)
    async def _do_shutdown(self) -> None:
        try:
            await self._agent.shutdown()
        except Exception:
            pass
        self.exit()

    @on(Input.Submitted, "#input-box")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user input submission."""
        user_input = event.value.strip()
        event.input.clear()

        if not user_input:
            return

        cmd = user_input.lower()

        # ── modal gate: when a page panel is showing, only allow :q, /x, and resume selection ──
        if self._page_mode is not None:
            if cmd == ":q":
                self._dismiss_overlay()
                return
            if cmd == "/x":
                self._dismiss_toast()
                return
            if self._showing_session_list and user_input.isdigit():
                self._handle_session_selection(user_input)
                return
            self._show_toast("当前在面板页，输入 :q 返回对话", duration=2)
            return

        if cmd == ":q":
            self._dismiss_overlay()
            return

        if cmd == "/x":
            self._dismiss_toast()
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

        if cmd == "/cron" or cmd.startswith("/cron "):
            self._handle_cron_cmd(user_input[5:].strip() if len(user_input) > 5 else "")
            return

        if cmd == "/skills" or cmd.startswith("/skills "):
            self._handle_skills_cmd(user_input[7:].strip() if len(user_input) > 7 else "")
            return

        if user_input.startswith(("/", ":")):
            self._show_toast(f"未知命令: {user_input}", duration=2)
            return

        # If previous response wasn't rated, treat the new message as implicit skip
        self._dismiss_feedback()

        # Show user message immediately
        chat_view = self.query_one("#chat-view", VerticalScroll)
        chat_view.mount(UserBubble(user_input))
        self._trim_chat_view(chat_view)

        # Start async response
        self._run_chat(user_input)

    @work(exclusive=True)
    async def _run_chat(self, user_input: str) -> None:
        """Run agent chat — streams response directly into the Alex bubble."""
        chat_view = self.query_one("#chat-view", VerticalScroll)

        # Create and mount the bubble immediately for streaming
        bubble = AlexBubble()
        chat_view.mount(bubble)
        chat_view.scroll_end()

        renderer = StreamRenderer(bubble)
        section_start = 0
        _last_ui_update = time.monotonic()
        _last_scroll = time.monotonic()

        try:
            async for event in self._agent.chat_stream(user_input):
                if isinstance(event, ThinkingUpdated):
                    renderer.on_thinking(event.delta)

                elif isinstance(event, TokenEmitted):
                    renderer.on_token(event.delta)

                elif isinstance(event, SkillLoaded):
                    renderer.on_skill_loaded(event.skill_name, event.skill_pattern)

                elif isinstance(event, ToolStarted):
                    tid = event.tool_id or f"{event.tool_name}:{time.monotonic_ns()}"
                    renderer.on_tool_started(tid, event.tool_name, event.tool_input)
                    section_start = len(renderer.collected)

                elif isinstance(event, ToolFinished):
                    renderer.on_tool_finished(event.tool_id or "", str(event.output or ""))

                # Throttle UI updates — ~50ms for smooth streaming
                now = time.monotonic()
                if now - _last_ui_update > 0.05:
                    section_text = renderer.collected[section_start:]
                    if section_text:
                        bubble.set_response(section_text)
                    elif renderer.thinking and section_start == 0:
                        bubble.set_response(f"  \U0001f4ad Thinking... ({len(renderer.thinking)} chars)")
                    if now - _last_scroll > 0.25:
                        chat_view.scroll_end()
                        _last_scroll = now
                    await asyncio.sleep(0)
                    _last_ui_update = now

        except Exception as e:
            bubble.finalize(ChatTurn(
                user_input=user_input,
                response=f"Error: {e}",
                thinking=renderer.thinking,
                tool_calls=list(renderer.tool_calls),
                skills=list(renderer.skills),
            ))
            return

        # Finalize the bubble with complete turn data
        result = self._agent.last_turn_result
        turn = ChatTurn(
            user_input=user_input,
            response=renderer.collected,
            thinking=renderer.thinking,
            tool_calls=list(renderer.tool_calls),
            skills=list(renderer.skills),
        )
        self._history.add(turn, messages_delta=getattr(result, 'message_batch', None))
        bubble.finalize(turn)
        if result:
            self._pending_feedback_turn_id = getattr(result, 'turn_id', '')

        # Show feedback prompt only when skills were actually used
        if renderer.skills:
            self._show_feedback_prompt()

        self._refresh_status_bar()
