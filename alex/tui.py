"""Textual TUI for Alex agent — alternate screen with scrollable chat."""

from __future__ import annotations

import asyncio
import json
import time

from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Input, Static, OptionList
from textual.widgets.option_list import Option
from textual.reactive import reactive


# ── Data model ───────────────────────────────────────────────────────────────

SESSIONS_DIR = Path.home() / ".alex" / "sessions"


@dataclass
class ChatTurn:
    """One turn of conversation."""
    user_input: str
    response: str = ""
    thinking: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    skills: list[dict] = field(default_factory=list)


@dataclass
class SessionMeta:
    """Metadata for a saved session."""
    session_id: str
    created_at: str  # ISO format
    first_message: str
    turn_count: int


class ChatHistory:
    """Persists chat sessions to ~/.alex/sessions/."""

    def __init__(self, session_id: str | None = None) -> None:
        self._turns: list[ChatTurn] = []
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        if session_id:
            self._session_id = session_id
        else:
            # New session
            self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._file = SESSIONS_DIR / f"{self._session_id}.json"
        self._meta: dict = {}

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def turns(self) -> list[ChatTurn]:
        return self._turns

    def add(self, turn: ChatTurn) -> None:
        self._turns.append(turn)
        self._save_deferred()

    def clear(self) -> None:
        self._turns.clear()
        self._save_deferred()

    def _save_deferred(self) -> None:
        """Save to disk via thread to avoid blocking the event loop."""
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self._save)
        except RuntimeError:
            self._save()  # no running loop, save synchronously

    def _save(self) -> None:
        first_msg = self._turns[0].user_input if self._turns else ""
        data = {
            "session_id": self._session_id,
            "created_at": self._meta.get("created_at", datetime.now().isoformat()),
            "first_message": first_msg,
            "turns": [asdict(t) for t in self._turns],
        }
        self._file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self) -> bool:
        """Load session from file. Returns True if loaded successfully."""
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                self._meta = data
                self._turns = [ChatTurn(**t) for t in data.get("turns", [])]
                return True
            except (json.JSONDecodeError, TypeError):
                self._turns = []
        return False

    @staticmethod
    def list_sessions() -> list[SessionMeta]:
        """List all saved sessions, newest first."""
        sessions = []
        if not SESSIONS_DIR.exists():
            return sessions
        for f in sorted(SESSIONS_DIR.glob("*.json"), reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                sessions.append(SessionMeta(
                    session_id=data.get("session_id", f.stem),
                    created_at=data.get("created_at", ""),
                    first_message=data.get("first_message", "")[:20],
                    turn_count=len(data.get("turns", [])),
                ))
            except (json.JSONDecodeError, TypeError):
                continue
        return sessions


# ── Widgets ──────────────────────────────────────────────────────────────────


class SystemBubble(Static):
    """System notification bubble."""

    DEFAULT_CSS = """
    SystemBubble {
        margin: 0;
        padding: 0 1;
        color: $text-muted;
        height: auto;
    }
    """

    def __init__(self, text: str) -> None:
        super().__init__(text)


class UserBubble(Static):
    """User message bubble."""

    DEFAULT_CSS = """
    UserBubble {
        margin: 1 0 0 0;
        padding: 0 1;
        border: round $secondary;
        border-title-color: $secondary;
        border-title-style: bold;
    }
    """

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.border_title = "You"


class ToolBubble(Vertical):
    """A single tool call rendered as a separate bubble below Alex's response."""

    def __init__(self, name: str, args: dict):
        super().__init__()
        self.border_title = f"\U0001f527 {name}"
        self._name = name
        self._args = args

    def compose(self) -> ComposeResult:
        lines = []
        for k, v in self._args.items():
            v_str = str(v)
            if len(v_str) > 80:
                v_str = v_str[:80] + "..."
            lines.append(f"├─ {k}: {v_str}")
        if lines:
            yield Static("\n".join(lines), id="tool-args")

    def set_done(self, output: str) -> None:
        self.border_title = f"\U0001f527 {self._name} ✓"
        out_line = str(output).split("\n")[0]
        if len(out_line) > 120:
            out_line = out_line[:120] + "..."
        self.mount(Static(f"└─ ✓ {out_line}", id="tool-output"))


class AlexBubble(Vertical):
    """One bubble per agent response — renders thinking, skills, and streamed text."""

    def __init__(self, turn: ChatTurn | None = None, thinking_expanded: bool = False, skills_expanded: bool = False) -> None:
        super().__init__()
        self._turn = turn or ChatTurn(user_input="", response="", thinking="", tool_calls=[], skills=[])
        self._thinking_expanded = thinking_expanded
        self._skills_expanded = skills_expanded
        self.border_title = "Alex"
        self._finalized = turn is not None
        self._current_response: Static | None = None

    def compose(self) -> ComposeResult:
        if self._finalized:
            yield from self._build_sections()
        else:
            self._current_response = Static("", classes="response-text")
            yield self._current_response

    def _build_sections(self):
        """Yield all section widgets based on current turn state."""
        turn = self._turn
        widgets: list[Static] = []

        # Skills
        if turn.skills:
            lines = []
            for sk in turn.skills:
                lines.append(f"◆ {sk.get('name', '')}")
                if sk.get("pattern"):
                    lines.append(f"  │ when: {sk['pattern']}")
            cls = "skills-expanded" if self._skills_expanded else "skills-expanded hidden"
            w = Static("\n".join(lines), classes=cls)
            w.border_title = "\U0001f3af Skills"
            widgets.append(w)

            names = [sk.get("name", "") for sk in turn.skills]
            total = len(turn.skills)
            cls = "skills-collapsed" if not self._skills_expanded else "skills-collapsed hidden"
            widgets.append(Static(
                f"\U0001f3af {total} skill{'s' if total > 1 else ''}: {', '.join(names)} [Ctrl+K]",
                classes=cls,
            ))

        # Thinking
        if turn.thinking:
            cls = "thinking-expanded" if self._thinking_expanded else "thinking-expanded hidden"
            w = Static(turn.thinking, classes=cls)
            w.border_title = "\U0001f4ad Thinking"
            widgets.append(w)

            char_count = len(turn.thinking)
            cls = "thinking-collapsed" if not self._thinking_expanded else "thinking-collapsed hidden"
            widgets.append(Static(
                f"\U0001f4ad Thinking ({char_count} chars) [Ctrl+T]",
                classes=cls,
            ))

        # Response
        if turn.response:
            widgets.append(Static(turn.response, classes="response-text"))

        return widgets

    def set_response(self, text: str) -> None:
        """Stream response text into the current response section."""
        self._turn.response = text
        if self._current_response:
            self._current_response.update(text)

    def insert_tool(self, name: str, args: dict) -> ToolBubble:
        """Insert a ToolBubble and keep assistant text below active tool output."""
        if self._current_response is not None:
            self._current_response.remove()
            self._current_response = None
        tb = ToolBubble(name, args)
        self.mount(tb)
        self._turn.response = ""
        self._current_response = Static("", classes="response-text")
        self.mount(self._current_response)
        return tb

    def finalize(self, turn: ChatTurn) -> None:
        """Rebuild bubble with full turn data after streaming completes."""
        self._turn = turn
        self._finalized = True
        self._current_response = None
        for child in list(self.children):
            child.remove()
        for widget in self._build_sections():
            # Keep the final layout aligned with the streaming layout:
            # tool calls render before the post-tool assistant response.
            if "response-text" in widget.classes and turn.tool_calls:
                for tc in turn.tool_calls:
                    name = tc.get("name", "")
                    args = tc.get("args", {})
                    tb = ToolBubble(name, args)
                    self.mount(tb)
                    output = tc.get("output", "")
                    if output:
                        tb.set_done(output)
            self.mount(widget)
        if turn.tool_calls and not turn.response:
            for tc in turn.tool_calls:
                name = tc.get("name", "")
                args = tc.get("args", {})
                tb = ToolBubble(name, args)
                self.mount(tb)
                output = tc.get("output", "")
                if output:
                    tb.set_done(output)

    def set_thinking_expanded(self, expanded: bool) -> None:
        """Toggle thinking visibility via CSS classes (no rebuild)."""
        self._thinking_expanded = expanded
        for w in self.query(".thinking-expanded"):
            w.set_class(not expanded, "hidden")
        for w in self.query(".thinking-collapsed"):
            w.set_class(expanded, "hidden")

    def set_skills_expanded(self, expanded: bool) -> None:
        """Toggle skills visibility via CSS classes (no rebuild)."""
        self._skills_expanded = expanded
        for w in self.query(".skills-expanded"):
            w.set_class(not expanded, "hidden")
        for w in self.query(".skills-collapsed"):
            w.set_class(expanded, "hidden")


# ── Main App ─────────────────────────────────────────────────────────────────


class AlexApp(App):
    """Alex Agent TUI — chat interface with scrollable history."""

    TITLE = "Alex"
    SUB_TITLE = "/help for shortcuts"

    CSS = """
    #main {
        height: 1fr;
    }
    #chat-view {
        height: 1fr;
        overflow-y: scroll;
        padding: 0 1;
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

    def __init__(self, agent, **kwargs) -> None:
        super().__init__(**kwargs)
        self._agent = agent
        self._history = ChatHistory()  # New session by default
        self._thinking_expanded = False
        self._skills_expanded = False
        self._showing_session_list = False
        self._last_response_rated = True  # start as rated (no pending feedback)
        self._feedback_widget: Static | None = None
        self._toast_widget: Static | None = None
        self._toast_timer: object = None
        self._status_timer: object = None
        self._cron_streams: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield VerticalScroll(id="chat-view")
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
        try:
            asyncio.create_task(self._agent.start_services())
        except Exception:
            pass
        self._status_timer = self.set_interval(0.1, self._poll_notifications)
        self._poll_notifications()

    def action_quit(self) -> None:
        try:
            if self._status_timer:
                self._status_timer.stop()
        except Exception:
            pass
        try:
            asyncio.create_task(self._agent.shutdown())
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

        if cmd == "/skills" or cmd.startswith("/skills "):
            self._handle_skills_cmd(user_input[7:].strip() if len(user_input) > 7 else "")
            return

        # If session list is showing and user typed a number, select that session
        if self._showing_session_list:
            self._handle_session_selection(user_input)
            return

        if not user_input.startswith(("/", ":")):
            chat_view = self.query_one("#chat-view", VerticalScroll)
            if len(chat_view.query("#help-block")) or len(chat_view.query("#skills-block")):
                self._show_toast("当前在面板页，输入 :q 返回对话", duration=2)
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

        collected = ""
        collected_thinking = ""
        tool_calls: list[dict] = []
        skills: list[dict] = []
        inflight_tools: dict[str, dict] = {}
        inflight_bubbles: dict[str, ToolBubble] = {}
        inflight_order: list[str] = []
        section_start = 0  # index into `collected` where current display section begins
        _last_ui_update = time.monotonic()
        _last_scroll = time.monotonic()

        try:
            async for event in self._agent.chat_stream(user_input):
                if event.type == "thinking":
                    collected_thinking += event.data

                elif event.type == "token":
                    collected += event.data

                elif event.type == "skill_load":
                    skills.append(event.data) if event.data else None

                elif event.type == "tool_start":
                    tid = str((event.data or {}).get("id") or "")
                    name = event.data.get("name", "")
                    args = event.data.get("input", {})
                    if not isinstance(args, dict):
                        args = {"input": str(args)}
                    if not tid:
                        tid = f"{name}:{time.monotonic_ns()}"
                    inflight_tools[tid] = {"id": tid, "name": name, "args": args, "output": ""}
                    inflight_order.append(tid)
                    inflight_bubbles[tid] = bubble.insert_tool(name, args)
                    section_start = len(collected)

                elif event.type == "tool_end":
                    output_str = str(event.data.get("output", ""))
                    tid = str((event.data or {}).get("id") or "")
                    if not tid or tid not in inflight_tools:
                        while inflight_order and inflight_order[-1] not in inflight_tools:
                            inflight_order.pop()
                        tid = inflight_order.pop() if inflight_order else ""
                    if tid and tid in inflight_tools:
                        inflight_tools[tid]["output"] = output_str
                        tool_calls.append(inflight_tools.pop(tid))
                        try:
                            inflight_order.remove(tid)
                        except ValueError:
                            pass
                    tb = inflight_bubbles.pop(tid, None) if tid else None
                    if tb:
                        tb.set_done(output_str)

                elif event.type == "done":
                    break

                # Throttle UI updates — ~50ms for smooth streaming
                now = time.monotonic()
                if now - _last_ui_update > 0.05:
                    section_text = collected[section_start:]
                    if section_text:
                        bubble.set_response(section_text)
                    elif collected_thinking and section_start == 0:
                        bubble.set_response(f"  \U0001f4ad Thinking... ({len(collected_thinking)} chars)")
                    if now - _last_scroll > 0.25:
                        chat_view.scroll_end()
                        _last_scroll = now
                    await asyncio.sleep(0)
                    _last_ui_update = now

        except Exception as e:
            bubble.finalize(ChatTurn(
                user_input=user_input,
                response=f"Error: {e}",
                thinking=collected_thinking,
                tool_calls=tool_calls,
                skills=skills,
            ))
            return

        # Finalize the bubble with complete turn data
        turn = ChatTurn(
            user_input=user_input,
            response=collected,
            thinking=collected_thinking,
            tool_calls=tool_calls,
            skills=skills,
        )
        self._history.add(turn)
        bubble.finalize(turn)

        # Show feedback prompt only when skills were actually used
        if skills:
            self._show_feedback_prompt()

        # Run reflection in foreground after conversation turn
        await self._run_reflection()

    async def _run_reflection(self) -> None:
        """Run skill reflection."""
        await self._agent._maybe_reflect()

        chat_view = self.query_one("#chat-view", VerticalScroll)
        self._trim_chat_view(chat_view)
        chat_view.scroll_end()

        # Show reflection result as system message
        self._poll_notifications()

    def _render_turn(self, turn: ChatTurn) -> None:
        """Render a full turn — used for history restore."""
        chat_view = self.query_one("#chat-view", VerticalScroll)
        chat_view.mount(UserBubble(turn.user_input))
        bubble = AlexBubble(turn, thinking_expanded=self._thinking_expanded, skills_expanded=self._skills_expanded)
        chat_view.mount(bubble)

    # ── feedback ─────────────────────────────────────────────────────────────

    def _show_feedback_prompt(self) -> None:
        """Show inline feedback prompt after a response."""
        self._last_response_rated = False
        if self._feedback_widget:
            self._feedback_widget.remove()
        chat_view = self.query_one("#chat-view", VerticalScroll)
        prompt = Static(
            "\U0001f44d Ctrl+G Good  \U0001f44e Ctrl+B Bad  ⏎ skip",
            classes="feedback-prompt",
        )
        self._feedback_widget = prompt
        chat_view.mount(prompt)

    def _dismiss_feedback(self) -> None:
        """Dismiss the feedback prompt without rating (implicit skip)."""
        self._last_response_rated = True
        if self._feedback_widget:
            self._feedback_widget.remove()
            self._feedback_widget = None

    def _rate_response(self, good: bool) -> None:
        """Submit user rating for the last response."""
        if self._last_response_rated:
            return
        self._last_response_rated = True

        self._agent.provide_feedback(good)
        if not good:
            self._show_toast("已标记为不满意，正在反思…", duration=2)

        # Replace prompt with confirmation
        if self._feedback_widget:
            self._feedback_widget.remove()
        label = "✓ Rated as helpful" if good else "✗ Rated as unhelpful"
        self._feedback_widget = Static(label, classes="feedback-done")
        self.query_one("#chat-view", VerticalScroll).mount(self._feedback_widget)

    def action_rate_good(self) -> None:
        self._rate_response(True)

    def action_rate_bad(self) -> None:
        self._rate_response(False)

    # ── toggles ──────────────────────────────────────────────────────────────

    def action_toggle_thinking(self) -> None:
        """Toggle all thinking blocks expanded/collapsed."""
        self._thinking_expanded = not self._thinking_expanded
        for bubble in self.query(AlexBubble):
            bubble.set_thinking_expanded(self._thinking_expanded)

    def action_toggle_skills(self) -> None:
        """Toggle all skill blocks expanded/collapsed."""
        self._skills_expanded = not self._skills_expanded
        for bubble in self.query(AlexBubble):
            bubble.set_skills_expanded(self._skills_expanded)

    # ── commands ────────────────────────────────────────────────────────────

    def _dismiss_overlay(self) -> None:
        """Remove overlay blocks (help, skills list, session list) and toast."""
        self._dismiss_panels()
        self._dismiss_toast()

    def _dismiss_panels(self) -> None:
        """Remove overlay blocks (help, skills list, session list)."""
        chat_view = self.query_one("#chat-view", VerticalScroll)
        for wid in ("help-block", "skills-block", "session-list"):
            try:
                chat_view.query_one(f"#{wid}").remove()
            except Exception:
                pass
        self._showing_session_list = False
        chat_view.scroll_end()

    def _dismiss_toast(self) -> None:
        """Hide the toast notification."""
        if self._toast_widget:
            self._toast_widget.set_class(True, "toast-hidden")
        if self._toast_timer:
            self._toast_timer.stop()
            self._toast_timer = None

    def _show_toast(self, message: str, duration: float = 2) -> None:
        """Show a small notification toast, auto-dismiss after `duration` seconds."""
        if self._toast_widget is None:
            self._toast_widget = Static("", classes="toast toast-hidden")
            self.mount(self._toast_widget)
        self._toast_widget.update(message)
        self._toast_widget.set_class(False, "toast-hidden")
        self._toast_timer = self.set_timer(duration, self._dismiss_toast)

    def _format_reflect_toast(self, note: dict) -> str:
        new = int(note.get("new", 0) or 0)
        updated = int(note.get("updated", 0) or 0)
        deprecated = int(note.get("deprecated", 0) or 0)
        names = note.get("names") or []
        if not isinstance(names, list):
            names = [str(names)]

        base = f"反思完成：新增 {new}，更新 {updated}，废弃 {deprecated}"
        if names:
            shown = ", ".join([str(n) for n in names[:3] if n])
            more = "…" if len(names) > 3 else ""
            base += f"（新技能：{shown}{more}）"
        return base

    def _poll_notifications(self) -> None:
        """Poll agent notifications and update UI (status bar + chat)."""
        chat_view = self.query_one("#chat-view", VerticalScroll)
        last_reflect: dict | None = None
        last_error: dict | None = None

        for note in self._agent.pop_notifications():
            ntype = note.get("type")
            stream_id = str(note.get("stream_id") or "")

            if ntype == "cron_debug":
                msg = str(note.get("message") or "")
                if msg:
                    self._show_toast(msg, duration=3)
                continue

            if ntype == "skill_reflect_error":
                last_error = note
                continue

            if ntype == "skill_reflect":
                last_reflect = note
                parts = []
                if int(note.get("new", 0) or 0) > 0:
                    parts.append(f"{note['new']} new: {', '.join(note.get('names') or [])}")
                if int(note.get("updated", 0) or 0) > 0:
                    parts.append(f"{note['updated']} updated")
                if int(note.get("deprecated", 0) or 0) > 0:
                    parts.append(f"{note['deprecated']} deprecated")
                if parts:
                    chat_view.mount(SystemBubble(
                        f"\U0001f3af Skills refined — {'; '.join(parts)}"
                    ))
                continue

            if ntype in ("cron_job_update", "cron_job_done"):
                if ntype == "cron_job_done":
                    job = note.get("job") or {}
                    name = str(job.get("name", "job"))
                    status = str(note.get("run_status") or job.get("status", ""))
                    if bool(job.get("subscribe")):
                        try:
                            asyncio.create_task(self._agent._stream_cron_reply(note))
                        except Exception:
                            pass
                    if status == "FAILED":
                        self._show_toast(f"任务失败：{name}", duration=3)
                    elif status == "SUCCESS":
                        self._show_toast(f"任务完成：{name}", duration=2)
                continue

            if ntype == "cron_stream_start" and stream_id:
                bubble = AlexBubble()
                chat_view.mount(bubble)
                chat_view.scroll_end()
                self._cron_streams[stream_id] = {
                    "bubble": bubble,
                    "collected": "",
                    "thinking": "",
                    "tool_calls": [],
                    "inflight_tools": {},
                    "inflight_bubbles": {},
                    "inflight_order": [],
                }
                continue

            if ntype == "cron_stream_tool_start" and stream_id:
                state = self._cron_streams.get(stream_id)
                if not state:
                    continue
                data = note.get("data") or {}
                tid = str(data.get("id") or "")
                name = str(data.get("name") or "")
                args = data.get("input", {})
                if not isinstance(args, dict):
                    args = {"input": str(args)}
                if not tid:
                    tid = f"{name}:{time.monotonic_ns()}"
                state["inflight_tools"][tid] = {"id": tid, "name": name, "args": args, "output": ""}
                state["inflight_order"].append(tid)
                state["inflight_bubbles"][tid] = state["bubble"].insert_tool(name, args)
                continue

            if ntype == "cron_stream_tool_end" and stream_id:
                state = self._cron_streams.get(stream_id)
                if not state:
                    continue
                data = note.get("data") or {}
                output_str = str(data.get("output", ""))
                tid = str(data.get("id") or "")
                if not tid or tid not in state["inflight_tools"]:
                    while state["inflight_order"] and state["inflight_order"][-1] not in state["inflight_tools"]:
                        state["inflight_order"].pop()
                    tid = state["inflight_order"].pop() if state["inflight_order"] else ""
                if tid and tid in state["inflight_tools"]:
                    state["inflight_tools"][tid]["output"] = output_str
                    state["tool_calls"].append(state["inflight_tools"].pop(tid))
                    try:
                        state["inflight_order"].remove(tid)
                    except ValueError:
                        pass
                tb = state["inflight_bubbles"].pop(tid, None) if tid else None
                if tb:
                    tb.set_done(output_str)
                continue

            if ntype == "cron_stream_thinking" and stream_id:
                state = self._cron_streams.get(stream_id)
                if not state:
                    continue
                state["thinking"] += str(note.get("data") or "")
                continue

            if ntype == "cron_stream_token" and stream_id:
                state = self._cron_streams.get(stream_id)
                if not state:
                    continue
                state["collected"] += str(note.get("data") or "")
                state["bubble"].set_response(state["collected"])
                continue

            if ntype == "cron_stream_done" and stream_id:
                state = self._cron_streams.pop(stream_id, None)
                if not state:
                    continue
                turn = ChatTurn(
                    user_input="",
                    response=state["collected"],
                    thinking=state["thinking"],
                    tool_calls=state["tool_calls"],
                    skills=[],
                )
                state["bubble"].finalize(turn)
                self._trim_chat_view(chat_view)
                chat_view.scroll_end()
                continue

            if ntype == "cron_stream_error" and stream_id:
                state = self._cron_streams.pop(stream_id, None)
                err = str(note.get("error") or "")
                if state:
                    state["bubble"].finalize(ChatTurn(user_input="", response=f"Error: {err}", thinking="", tool_calls=state["tool_calls"], skills=[]))
                else:
                    chat_view.mount(SystemBubble(f"cron error: {err}"))
                self._trim_chat_view(chat_view)
                chat_view.scroll_end()
                continue

        if last_reflect is not None:
            self._show_toast(self._format_reflect_toast(last_reflect), duration=3)
        if last_error is not None:
            self._show_toast(f"反思失败：{last_error.get('error', '')}", duration=4)
        self._refresh_status_bar()

    def _refresh_status_bar(self) -> None:
        try:
            jobs = self._agent.list_cron_jobs()
        except Exception:
            jobs = []

        lines: list[str] = []
        if not jobs:
            lines.append("  [无任务]")
        else:
            now = time.time()
            for j in jobs[:20]:
                name = str(j.get("name", ""))[:18]
                status = str(j.get("status", ""))
                icon = {
                    "RUNNING": "⟳",
                    "SCHEDULED": "⏱",
                    "SUCCESS": "✓",
                    "FAILED": "✗",
                    "CANCELLED": "⦸",
                }.get(status, "·")
                next_at = j.get("next_run_at")
                if isinstance(next_at, (int, float)) and next_at:
                    eta = max(0, int(next_at - now))
                    eta_s = f"{eta}s"
                else:
                    eta_s = "-"
                runs = int(j.get("runs_done", 0) or 0)
                lines.append(f"{icon} {name}  ({status})  next:{eta_s}  ran:{runs}")

        content = self.query_one("#status-content", Static)
        content.update("\n".join(lines))

    def _trim_chat_view(self, chat_view: VerticalScroll) -> None:
        """Keep only the last 40 widgets to prevent layout slowdown."""
        children = list(chat_view.children)
        if len(children) > 40:
            for child in children[:-40]:
                child.remove()

    def _show_help(self) -> None:
        """Show help with all commands and keyboard shortcuts."""
        chat_view = self.query_one("#chat-view", VerticalScroll)
        help_text = """  \U0001f4d6 Commands:
    /help             Show this help
    /skills           List all skills
    /skills del <id>  Delete a skill by name or id prefix
    /skills dep <id>  Deprecate a skill by name or id prefix
    /merge-skills     LLM-based skill deduplication
    /reflect          Force skill reflection now
    /resume           Resume a saved session
    /clear            Clear current chat
    /quit             Exit Alex
    :q                Return to chat
    /x                Dismiss toast

  ⌨️  Shortcuts:
    Ctrl+G / Ctrl+B   Rate last response (Good / Bad)
    Ctrl+T            Toggle thinking blocks
    Ctrl+K            Toggle skill blocks"""
        chat_view.mount(Static(help_text, id="help-block"))
        chat_view.scroll_end()

    @work(exclusive=True)
    async def _run_force_reflection(self) -> None:
        self._show_toast("正在反思…", duration=2)
        await self._agent._do_reflect()
        self._poll_notifications()
        chat_view = self.query_one("#chat-view", VerticalScroll)
        self._trim_chat_view(chat_view)
        chat_view.scroll_end()

    def _handle_skills_cmd(self, args: str) -> None:
        """Handle /skills [del|dep] [id]"""
        store = self._agent._skills.store
        chat_view = self.query_one("#chat-view", VerticalScroll)

        if not args:
            # List all skills
            all_skills = store.list_all()
            if not all_skills:
                chat_view.mount(Static("  [No skills]"))
            else:
                lines = ["  \U0001f3af Skills:"]
                for s in sorted(all_skills, key=lambda s: (s.status, -s.use_count)):
                    status_icon = {"ACTIVE": "✅", "CANDIDATE": "\U0001f535", "DEPRECATED": "⚪"}.get(s.status, "?")
                    lines.append(
                        f"  {status_icon} [{s.status}] {s.name}"
                        f"  | used:{s.use_count} ok:{s.success_count} fail:{s.failure_count}"
                        f"  | id:{s.id[:8]}"
                    )
                chat_view.mount(Static("\n".join(lines), id="skills-block"))
            chat_view.scroll_end()
            return

        parts = args.split(None, 1)
        action = parts[0].lower()
        target = parts[1] if len(parts) > 1 else ""

        if action in ("del", "delete") and target:
            found = None
            for s in store.list_all():
                if s.id.startswith(target) or s.name.lower() == target.lower():
                    found = s
                    break
            if found:
                store.remove(found.id)
                chat_view.mount(Static(f"  ✅ Deleted: {found.name}"))
            else:
                chat_view.mount(Static(f"  ❌ Not found: {target}"))
        elif action in ("dep", "deprecate") and target:
            found = None
            for s in store.list_all():
                if s.id.startswith(target) or s.name.lower() == target.lower():
                    found = s
                    break
            if found:
                store.deprecate(found.id)
                chat_view.mount(Static(f"  ✅ Deprecated: {found.name}"))
            else:
                chat_view.mount(Static(f"  ❌ Not found: {target}"))
        else:
            chat_view.mount(Static(f"  ❌ Unknown: /skills {args}"))

        chat_view.scroll_end()

    # ── session management ───────────────────────────────────────────────────

    def _show_session_list(self) -> None:
        """Show a list of saved sessions for the user to pick from."""
        sessions = ChatHistory.list_sessions()
        chat_view = self.query_one("#chat-view", VerticalScroll)

        if not sessions:
            chat_view.mount(Static("  [No saved sessions found]"))
            chat_view.scroll_end()
            return

        self._showing_session_list = True
        self._session_options = sessions

        lines = ["\U0001f4cb Saved sessions (type number to resume, or anything else to cancel):", ""]
        for i, s in enumerate(sessions, 1):
            # Parse created_at for display
            try:
                dt = datetime.fromisoformat(s.created_at)
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                time_str = s.created_at[:16] if s.created_at else "unknown"
            preview = s.first_message if s.first_message else "(empty)"
            lines.append(f"  {i}. [{time_str}] {preview}  ({s.turn_count} turns)")

        chat_view.mount(Static("\n".join(lines), id="session-list"))
        chat_view.scroll_end()

    def _handle_session_selection(self, user_input: str) -> None:
        """Handle user's session selection."""
        self._showing_session_list = False

        # Remove the session list widget
        try:
            self.query_one("#session-list").remove()
        except Exception:
            pass

        # Check if input is a valid number
        try:
            idx = int(user_input) - 1
            if 0 <= idx < len(self._session_options):
                session = self._session_options[idx]
                self._resume_session(session.session_id)
                return
        except (ValueError, AttributeError):
            pass

        # Not a valid selection — just show a cancel message
        chat_view = self.query_one("#chat-view", VerticalScroll)
        chat_view.mount(Static("  [Resume cancelled]"))
        chat_view.scroll_end()

    def _resume_session(self, session_id: str) -> None:
        """Resume a saved session."""
        # Clear current view
        chat_view = self.query_one("#chat-view", VerticalScroll)
        chat_view.remove_children()

        # Load the session
        self._history = ChatHistory(session_id=session_id)
        self._history.load()

        # Render all turns
        for turn in self._history.turns:
            self._render_turn(turn)

        chat_view.scroll_end()

        # Also restore agent memory
        import asyncio
        asyncio.ensure_future(self._restore_agent_memory())

    async def _restore_agent_memory(self) -> None:
        """Restore agent memory from loaded session."""
        from langchain_core.messages import AIMessage, HumanMessage
        await self._agent.clear_history()
        for turn in self._history.turns:
            await self._agent._memory.add_message(HumanMessage(content=turn.user_input))
            if turn.response:
                kwargs = {}
                if turn.thinking:
                    kwargs["reasoning_content"] = turn.thinking
                await self._agent._memory.add_message(
                    AIMessage(content=turn.response, additional_kwargs=kwargs)
                )

    def _clear_chat(self) -> None:
        """Clear chat history and view."""
        self._history.clear()
        chat_view = self.query_one("#chat-view", VerticalScroll)
        chat_view.remove_children()

        import asyncio
        asyncio.ensure_future(self._agent.clear_history())

    @work(exclusive=True)
    async def _run_merge_skills(self) -> None:
        """Run LLM-based skill merging."""
        chat_view = self.query_one("#chat-view", VerticalScroll)

        # Show status
        before_count = len([s for s in self._agent._skills.store.list_all() if s.status != "DEPRECATED"])
        status_widget = Static(f"  \U0001f527 Merging skills... ({before_count} skills, this may take a moment)")
        chat_view.mount(status_widget)
        chat_view.scroll_end()

        try:
            result = await self._agent._skills.merge_skills(self._agent._llm)
            status_widget.remove()

            # Show result
            msg = (
                f"  ✓ Skill merge complete:\n"
                f"    Merged: {result.get('merged', 0)} redundant skills removed\n"
                f"    Deprecated: {result.get('deprecated', 0)} skills deprecated\n"
                f"    Remaining: {result.get('remaining', '?')} active skills"
            )
            if result.get("error"):
                msg += f"\n    ⚠ {result['error']}"
            chat_view.mount(Static(msg))
        except Exception as e:
            status_widget.remove()
            chat_view.mount(Static(f"  ✗ Merge failed: {e}"))

        chat_view.scroll_end()
