"""Textual TUI for Alex agent — alternate screen with scrollable chat."""

from __future__ import annotations

import asyncio
import threading
import time

from datetime import datetime
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Input, Static, OptionList
from textual.widgets.option_list import Option
from textual.reactive import reactive

from alex.events import CronDebugEvent, CronJobEvent, SkillReflectErrorEvent, SkillReflectEvent
from alex.streaming.handler import StreamEvent
import alex.session as session_store


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class ChatTurn:
    """One turn of conversation — UI view-model derived from message sequences."""
    user_input: str
    response: str = ""
    thinking: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    skills: list[dict] = field(default_factory=list)
    kind: str = "user"  # "user" | "cron" — controls whether _render_turn shows a UserBubble


def _parse_load_skill_output(output: str) -> dict | None:
    """Extract {name, pattern} from a load_skill tool output string.

    The output format is:
        [Skill: <name>]
        When to apply: <pattern>
        Execution methodology:
        <instruction>
    """
    if not output.startswith("[Skill:"):
        return None
    lines = output.split("\n")
    name = lines[0].removeprefix("[Skill:").removesuffix("]").strip() if lines else ""
    pattern = ""
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.lower().startswith("when to apply:"):
            pattern = stripped.split(":", 1)[1].strip()
            break
    return {"name": name, "pattern": pattern} if name else None


def _messages_to_turns(messages: list[BaseMessage]) -> tuple[list[ChatTurn], list[BaseMessage]]:
    """Convert a message sequence to UI view-models.

    Uses tool_call_id → dict mapping so that multi-tool AIMessages are
    correctly paired with their ToolMessage outputs.  A single-pointer
    pending_tool would lose all but the last tool call per message.

    Returns (turns, messages) — messages pass through unchanged so
    Agent.restore_history() gets the exact sequence.
    """
    turns: list[ChatTurn] = []
    current: ChatTurn | None = None
    pending: dict[str, dict] = {}     # tool_call_id → tool_call dict
    _order: list[str] = []             # insertion order for fallback

    for msg in messages:
        if isinstance(msg, HumanMessage):
            if current is not None:
                turns.append(current)
            current = ChatTurn(user_input=str(msg.content), kind="user")
            pending.clear()
            _order.clear()

        elif isinstance(msg, AIMessage) and msg.tool_calls:
            ak = getattr(msg, "additional_kwargs", None)
            turn_start = bool(isinstance(ak, dict) and ak.get("alex_turn_start"))
            turn_kind = str(ak.get("alex_turn_kind", "cron")) if isinstance(ak, dict) else "cron"
            if turn_start and current is not None:
                turns.append(current)
                current = None
                pending.clear()
                _order.clear()
            if current is None:
                current = ChatTurn(user_input="", kind=turn_kind)
            for tc in msg.tool_calls:
                tc_id = str(tc.get("id", ""))
                tc_dict = {
                    "name": tc.get("name", ""),
                    "args": tc.get("args", {}),
                    "id": tc_id,
                    "output": "",
                }
                current.tool_calls.append(tc_dict)
                if tc_id:
                    pending[tc_id] = tc_dict
                    _order.append(tc_id)

        elif isinstance(msg, ToolMessage):
            tc_id = str(getattr(msg, "tool_call_id", ""))
            matched = pending.pop(tc_id, None) if tc_id else None
            if matched is not None:
                matched["output"] = str(msg.content)
                if tc_id in _order:
                    _order.remove(tc_id)
                # Recover skill metadata from load_skill tool output
                if matched.get("name") == "load_skill":
                    skill_info = _parse_load_skill_output(str(msg.content))
                    if skill_info:
                        current.skills.append(skill_info)
            elif _order:
                # fallback: match oldest unmatched tool call
                fallback_id = _order.pop(0)
                fb = pending.pop(fallback_id, None)
                if fb is not None:
                    fb["output"] = str(msg.content)

        elif isinstance(msg, AIMessage) and not msg.tool_calls:
            if current is None:
                current = ChatTurn(user_input="", kind="cron")
            current.response = str(msg.content)
            ak = getattr(msg, "additional_kwargs", None)
            if ak and isinstance(ak, dict):
                current.thinking = ak.get("reasoning_content", "") or ""

    if current is not None:
        turns.append(current)
    return turns, messages


class ChatHistory:
    """UI-side session bookkeeping — delegates persistence to alex.session.

    Maintains *both* a ChatTurn list (for rendering) and an authoritative
    message sequence (for persistence).  New turns are added with the
    exact message delta produced by Agent.chat_stream() / cron reply, so
    _save() writes the precise message sequence — never a ChatTurn
    reverse-engineering.

    Thread safety: _save() uses a version counter so that only the most
    recent write actually hits disk.
    """

    def __init__(self, session_id: str | None = None) -> None:
        self._turns: list[ChatTurn] = []
        self._messages: list[BaseMessage] = []
        self._cron_history: list[dict] = []

        if session_id:
            self._session_id = session_id
        else:
            self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._meta: dict = {}
        self._save_lock = threading.Lock()
        self._save_version = 0

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def turns(self) -> list[ChatTurn]:
        return self._turns

    @property
    def loaded_messages(self) -> list[BaseMessage]:
        """The authoritative message sequence — for Agent.restore_history()."""
        return self._messages

    @property
    def cron_history(self) -> list[dict]:
        return self._cron_history

    def add(self, turn: ChatTurn, messages_delta: list[BaseMessage] | None = None) -> None:
        """Record a turn with its exact message delta from the Agent.

        messages_delta is the list of BaseMessage objects that Agent just
        wrote to memory for this turn.  It is appended verbatim to the
        authoritative sequence — no reverse-engineering from ChatTurn.
        """
        self._turns.append(turn)
        if messages_delta:
            self._messages.extend(messages_delta)
        self._save_deferred()

    def add_cron_record(self, record: dict) -> None:
        execution_id = str(record.get("execution_id", ""))
        if execution_id and any(str(item.get("execution_id", "")) == execution_id for item in self._cron_history):
            return
        self._cron_history.append(record)
        self._save_deferred()

    def clear(self) -> None:
        self._turns.clear()
        self._messages.clear()
        self._cron_history.clear()
        self._save_deferred()

    def _save_deferred(self) -> None:
        self._save_version += 1
        v = self._save_version
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self._save, v)
        except RuntimeError:
            self._save(v)

    def _save(self, version: int = 0) -> None:
        with self._save_lock:
            if version and version < self._save_version:
                return
            session_store.save_session_bundle(self._session_id, self._messages, self._cron_history)

    def load(self) -> bool:
        """Load session from disk.  Returns True if the file was parsed successfully.

        An empty message list is a valid session (produced by /clear).
        """
        bundle = session_store.load_session_bundle(self._session_id)
        if bundle is not None:
            self._turns, self._messages = _messages_to_turns(bundle["messages"])
            self._cron_history = list(bundle.get("cron_history", []) or [])
            return True
        return False


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

    def __init__(self, agent, **kwargs) -> None:
        super().__init__(**kwargs)
        self._agent = agent
        self._history = ChatHistory()  # New session by default
        self._thinking_expanded = False
        self._skills_expanded = False
        self._showing_session_list = False
        self._page_mode: str | None = None
        self._last_response_rated = True  # start as rated (no pending feedback)
        self._feedback_widget: Static | None = None
        self._toast_widget: Static | None = None
        self._toast_timer: object = None
        self._status_timer: object = None
        self._cron_streams: dict[str, dict] = {}

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
        self._start_services()
        self._status_timer = self.set_interval(0.1, self._poll_notifications)
        self._poll_notifications()

    @work(exclusive=True)
    async def _start_services(self) -> None:
        try:
            await self._agent.start_services()
        except Exception:
            pass

    def action_quit(self) -> None:
        if self._status_timer:
            self._status_timer.stop()
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

        # If session list is showing and user typed a number, select that session
        if self._showing_session_list:
            self._handle_session_selection(user_input)
            return

        if not user_input.startswith(("/", ":")):
            if self._page_mode is not None:
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
        _message_batch: list[BaseMessage] | None = None

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

                elif event.type == "message_batch":
                    _message_batch = event.data if isinstance(event.data, list) else None

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
        self._history.add(turn, messages_delta=_message_batch)
        bubble.finalize(turn)

        # Show feedback prompt only when skills were actually used
        if skills:
            self._show_feedback_prompt()

        # Show reflection result as system message (reflection triggered by agent internally)
        self._poll_notifications()

    def _render_turn(self, turn: ChatTurn) -> None:
        """Render a full turn via finalize() — same path as live streaming.

        Creating AlexBubble(turn) directly skips finalize(), which means
        ToolBubble widgets are never mounted.  We instead create an empty
        streaming bubble and call finalize(turn) to reproduce the full
        live-chat rendering (skills, thinking, tool blocks, response).
        """
        chat_view = self.query_one("#chat-view", VerticalScroll)
        if turn.kind != "cron":
            chat_view.mount(UserBubble(turn.user_input))
        bubble = AlexBubble(thinking_expanded=self._thinking_expanded, skills_expanded=self._skills_expanded)
        chat_view.mount(bubble)
        bubble.finalize(turn)

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
        page_view = self.query_one("#page-view", VerticalScroll)
        chat_view.remove_class("hidden")
        page_view.add_class("hidden")
        self.query_one("#page-title", Static).update("")
        self.query_one("#page-content", Static).update("")
        self._showing_session_list = False
        self._page_mode = None
        chat_view.scroll_end()

    def _show_page(self, title: str, content: str, *, mode: str) -> None:
        self._page_mode = mode
        chat_view = self.query_one("#chat-view", VerticalScroll)
        page_view = self.query_one("#page-view", VerticalScroll)
        chat_view.add_class("hidden")
        page_view.remove_class("hidden")
        self.query_one("#page-title", Static).update(title)
        self.query_one("#page-content", Static).update(content)
        page_view.scroll_home()

    @staticmethod
    def _fmt_ts(ts: float | None) -> str:
        if not ts:
            return "-"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def _format_cron_page(self, records: list[dict], query: str = "") -> str:
        header = f"当前会话已完成 cron 执行记录 ({len(records)})"
        if query:
            header += f"\n筛选: {query}"
        header += "\n"
        if not records:
            return header + "\n  [无已完成任务]\n"
        lines = [header]
        for rec in records:
            result = str(rec.get("result") or rec.get("error") or "")
            if len(result) > 120:
                result = result[:120] + "..."
            params = str(rec.get("params", {}))
            if len(params) > 120:
                params = params[:120] + "..."
            lines.extend([
                f"- [{rec.get('execution_id', '')}] {rec.get('name', '')} ({rec.get('status', '')})",
                f"  job_id: {rec.get('job_id', '')}",
                f"  action: {rec.get('action', '')}",
                f"  started: {self._fmt_ts(rec.get('started_at'))}",
                f"  finished: {self._fmt_ts(rec.get('finished_at'))}",
                f"  params: {params}",
                f"  result: {result}",
                "",
            ])
        return "\n".join(lines).rstrip()

    def _persist_cron_record(self, event: CronJobEvent) -> None:
        if event.status not in ("SUCCESS", "FAILED"):
            return
        target_session_id = event.session_id or self._history.session_id
        record = {
            "execution_id": event.tool_call_id or f"cron:{event.job_id}:{event.runs_done}",
            "job_id": event.job_id,
            "name": event.name,
            "status": event.status,
            "action": event.action,
            "params": dict(event.params or {}),
            "runs_done": event.runs_done,
            "started_at": event.started_at,
            "finished_at": event.finished_at,
            "result": event.result,
            "error": event.error,
        }
        if target_session_id == self._history.session_id:
            self._history.add_cron_record(record)
            self._agent.set_session_context(self._history.session_id, self._history.cron_history)
            return
        session_store.append_cron_history(target_session_id, record)

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

    def _format_reflect_toast(self, evt: SkillReflectEvent) -> str:
        base = f"反思完成：新增 {evt.new}，更新 {evt.updated}，废弃 {evt.deprecated}"
        if evt.names:
            shown = ", ".join([str(n) for n in evt.names[:3] if n])
            more = "…" if len(evt.names) > 3 else ""
            base += f"（新技能：{shown}{more}）"
        if evt.updated_names:
            shown = ", ".join([str(n) for n in evt.updated_names[:3] if n])
            more = "…" if len(evt.updated_names) > 3 else ""
            base += f"（更新：{shown}{more}）"
        return base

    def _poll_notifications(self) -> None:
        """Poll agent events and update UI (status bar + chat).

        Dispatches on concrete event types instead of string-matching on
        bare dicts.  Cron stream events arrive as StreamEvent instances
        (same type as regular chat) keyed by stream_id in metadata.
        """
        chat_view = self.query_one("#chat-view", VerticalScroll)
        last_reflect: SkillReflectEvent | None = None
        last_error: SkillReflectErrorEvent | None = None

        for event in self._agent.pop_notifications():
            # ── system events ──────────────────────────────────────────────

            if isinstance(event, CronDebugEvent):
                if event.message:
                    self._show_toast(event.message, duration=3)
                continue

            if isinstance(event, SkillReflectErrorEvent):
                last_error = event
                continue

            if isinstance(event, SkillReflectEvent):
                last_reflect = event
                if event.new or event.updated or event.deprecated:
                    chat_view.mount(SystemBubble(f"\U0001f3af {event.toast}"))
                continue

            if isinstance(event, CronJobEvent):
                self._persist_cron_record(event)
                if event.status == "FAILED":
                    self._show_toast(f"任务失败：{event.name}", duration=3)
                elif event.status == "SUCCESS":
                    self._show_toast(f"任务完成：{event.name}", duration=2)
                continue

            # ── cron stream events (StreamEvent with stream_id in metadata) ──

            if isinstance(event, StreamEvent):
                meta = event.metadata or {}
                stream_id = str(meta.get("stream_id") or "")

                if not stream_id:
                    continue

                if meta.get("is_cron", False) and event.type == "tool_start":
                    # First tool_start for a cron stream — create bubble + state
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
                        "message_batch": None,
                    }
                    # Fall through to handle the tool_start itself
                    data = event.data or {}
                    tid = str(data.get("id") or stream_id)
                    name = str(data.get("name") or "")
                    args = data.get("input", {})
                    if not isinstance(args, dict):
                        args = {"input": str(args)}
                    state = self._cron_streams[stream_id]
                    state["inflight_tools"][tid] = {"id": tid, "name": name, "args": args, "output": ""}
                    state["inflight_order"].append(tid)
                    state["inflight_bubbles"][tid] = state["bubble"].insert_tool(name, args)
                    continue

                if event.type == "tool_start":
                    state = self._cron_streams.get(stream_id)
                    if not state:
                        continue
                    data = event.data or {}
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

                if event.type == "tool_end":
                    state = self._cron_streams.get(stream_id)
                    if not state:
                        continue
                    data = event.data or {}
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

                if event.type == "thinking":
                    state = self._cron_streams.get(stream_id)
                    if state:
                        state["thinking"] += str(event.data or "")
                    continue

                if event.type == "token":
                    state = self._cron_streams.get(stream_id)
                    if state:
                        state["collected"] += str(event.data or "")
                        state["bubble"].set_response(state["collected"])
                    continue

                if event.type == "message_batch":
                    state = self._cron_streams.get(stream_id)
                    if state and isinstance(event.data, list):
                        state["message_batch"] = event.data
                    continue

                if meta.get("is_cron_done"):
                    state = self._cron_streams.pop(stream_id, None)
                    if not state:
                        continue
                    turn = ChatTurn(
                        user_input="",
                        response=state["collected"],
                        thinking=state["thinking"],
                        tool_calls=state["tool_calls"],
                        skills=[],
                        kind="cron",
                    )
                    state["bubble"].finalize(turn)
                    self._history.add(turn, messages_delta=state.get("message_batch"))
                    self._trim_chat_view(chat_view)
                    chat_view.scroll_end()
                    continue

                if meta.get("is_cron_error"):
                    state = self._cron_streams.pop(stream_id, None)
                    err = str(event.data or "")
                    if state:
                        state["bubble"].finalize(ChatTurn(
                            user_input="", response=f"Error: {err}",
                            thinking="", tool_calls=state["tool_calls"], skills=[],
                            kind="cron",
                        ))
                    else:
                        chat_view.mount(SystemBubble(f"cron error: {err}"))
                    self._trim_chat_view(chat_view)
                    chat_view.scroll_end()
                    continue

        if last_reflect is not None:
            self._show_toast(self._format_reflect_toast(last_reflect), duration=3)
        if last_error is not None:
            self._show_toast(f"反思失败：{last_error.error}", duration=4)
        self._refresh_status_bar()

    def _refresh_status_bar(self) -> None:
        try:
            jobs = self._agent.list_cron_jobs()
        except Exception:
            jobs = []
        jobs = [j for j in jobs if str(j.get("status", "")) in ("RUNNING", "SCHEDULED")]

        lines: list[str] = []
        if not jobs:
            lines.append("  [无任务]")
        else:
            now = time.time()
            for j in jobs[:20]:
                name = str(j.get("name", ""))[:18]
                status = str(j.get("status", ""))
                icon = {"RUNNING": "⟳", "SCHEDULED": "⏱"}.get(status, "·")
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
        help_text = """  \U0001f4d6 Commands:
    /help             Show this help
    /skills           List all skills
    /cron [query]     Query completed cron executions in this session
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
        self._show_page("帮助", help_text, mode="help")

    @work(exclusive=True)
    async def _run_force_reflection(self) -> None:
        self._show_toast("正在反思…", duration=2)
        await self._agent.reflect()
        self._poll_notifications()
        chat_view = self.query_one("#chat-view", VerticalScroll)
        self._trim_chat_view(chat_view)
        chat_view.scroll_end()

    def _handle_skills_cmd(self, args: str) -> None:
        """Handle /skills [del|dep] [id]"""
        if not args:
            all_skills = self._agent.list_skills()
            if not all_skills:
                content = "  [No skills]"
            else:
                lines = ["  \U0001f3af Skills:"]
                for s in sorted(all_skills, key=lambda s: (s["status"], -s["use_count"])):
                    status_icon = {"ACTIVE": "✅", "CANDIDATE": "\U0001f535", "DEPRECATED": "⚪"}.get(s["status"], "?")
                    lines.append(
                        f"  {status_icon} [{s['status']}] {s['name']}"
                        f"  | used:{s['use_count']} ok:{s['success_count']} fail:{s['failure_count']}"
                        f"  | id:{s['id'][:8]}"
                    )
                content = "\n".join(lines)
            self._show_page("技能列表", content, mode="skills")
            return

        parts = args.split(None, 1)
        action = parts[0].lower()
        target = parts[1] if len(parts) > 1 else ""

        if action in ("del", "delete") and target:
            name = self._agent.delete_skill(target)
            if name:
                self._show_toast(f"已删除技能：{name}", duration=2)
            else:
                self._show_toast(f"未找到技能：{target}", duration=2)
        elif action in ("dep", "deprecate") and target:
            name = self._agent.deprecate_skill(target)
            if name:
                self._show_toast(f"已废弃技能：{name}", duration=2)
            else:
                self._show_toast(f"未找到技能：{target}", duration=2)
        else:
            self._show_toast(f"未知命令: /skills {args}", duration=2)

    def _handle_cron_cmd(self, args: str) -> None:
        """Show completed cron execution history for the current session."""
        records = self._agent.list_session_cron_history(query=args, limit=50)
        content = self._format_cron_page(records, query=args)
        self._show_page("Cron 历史", content, mode="cron")

    # ── session management ───────────────────────────────────────────────────

    def _show_session_list(self) -> None:
        """Show a list of saved sessions for the user to pick from."""
        sessions = session_store.list_sessions()

        if not sessions:
            self._show_page("会话列表", "  [No saved sessions found]", mode="resume")
            return

        self._showing_session_list = True
        self._session_options = sessions

        lines = ["\U0001f4cb Saved sessions (type number to resume, or anything else to cancel):", ""]
        for i, s in enumerate(sessions, 1):
            try:
                dt = datetime.fromisoformat(s.created_at)
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                time_str = s.created_at[:16] if s.created_at else "unknown"
            preview = s.first_message if s.first_message else "(empty)"
            lines.append(f"  {i}. [{time_str}] {preview}  ({s.message_count} msgs)")

        self._show_page("会话列表", "\n".join(lines), mode="resume")

    def _handle_session_selection(self, user_input: str) -> None:
        """Handle user's session selection."""
        self._showing_session_list = False

        # Check if input is a valid number
        try:
            idx = int(user_input) - 1
            if 0 <= idx < len(self._session_options):
                session = self._session_options[idx]
                self._resume_session(session.session_id)
                return
        except (ValueError, AttributeError):
            pass

        self._dismiss_panels()
        self._show_toast("已取消恢复会话", duration=2)

    @work(exclusive=True)
    async def _resume_session(self, session_id: str) -> None:
        """Resume a saved session — restore memory first, then render UI.

        Uses @work(exclusive=True) to serialize with other lifecycle ops
        (clear, merge).  Input is disabled during the operation to prevent
        the user sending a message before agent memory is ready.
        """
        # Load the session and restore agent memory first
        self._history = ChatHistory(session_id=session_id)
        ok = self._history.load()
        if not ok:
            return

        input_widget = self.query_one("#input-box", Input)
        input_widget.disabled = True
        try:
            await self._agent.restore_history(self._history.loaded_messages)
            self._agent.set_session_context(self._history.session_id, self._history.cron_history)

            # Now render UI with the restored state
            chat_view = self.query_one("#chat-view", VerticalScroll)
            chat_view.remove_children()
            for turn in self._history.turns:
                self._render_turn(turn)
            chat_view.scroll_end()
            self._dismiss_panels()
        finally:
            input_widget.disabled = False

    @work(exclusive=True)
    async def _clear_chat(self) -> None:
        """Clear chat history and view — memory first, then UI.

        Serialized via @work(exclusive=True); input is disabled during the
        operation so the user cannot send a message against stale state.
        """
        input_widget = self.query_one("#input-box", Input)
        input_widget.disabled = True
        try:
            await self._agent.clear_history()
            self._history.clear()
            self._agent.set_session_context(self._history.session_id, self._history.cron_history)
            chat_view = self.query_one("#chat-view", VerticalScroll)
            chat_view.remove_children()
            self._dismiss_panels()
        finally:
            input_widget.disabled = False

    @work(exclusive=True)
    async def _run_merge_skills(self) -> None:
        """Run LLM-based skill merging."""
        chat_view = self.query_one("#chat-view", VerticalScroll)

        # Show status
        before_count = len([s for s in self._agent.list_skills() if s["status"] != "DEPRECATED"])
        status_widget = Static(f"  \U0001f527 Merging skills... ({before_count} skills, this may take a moment)")
        chat_view.mount(status_widget)
        chat_view.scroll_end()

        try:
            result = await self._agent.merge_skills()
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
