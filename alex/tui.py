"""Textual TUI for Alex agent — alternate screen with scrollable chat."""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
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
        self._save()

    def clear(self) -> None:
        self._turns.clear()
        self._save()

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


class AlexBubble(Vertical):
    """Alex response bubble — contains thinking, tools, and response."""

    DEFAULT_CSS = """
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
    AlexBubble > .tools-collapsed {
        height: 1;
        color: $text-muted;
        padding: 0;
        margin: 0;
    }
    AlexBubble > .tools-expanded {
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
    """

    def __init__(self, turn: ChatTurn, thinking_expanded: bool = False, tools_expanded: bool = False) -> None:
        super().__init__()
        self._turn = turn
        self._thinking_expanded = thinking_expanded
        self._tools_expanded = tools_expanded
        self.border_title = "Alex"

    def compose(self) -> ComposeResult:
        turn = self._turn

        # Tool calls — both versions, toggle with CSS display
        if turn.tool_calls:
            # Expanded version
            lines = []
            for tc in turn.tool_calls:
                name = tc.get("name", "")
                lines.append(f"◈ {name}")
                for k, v in tc.get("args", {}).items():
                    v_str = str(v)
                    if len(v_str) > 60:
                        v_str = v_str[:60] + "..."
                    lines.append(f"  ├─ {k}: {v_str}")
                output = tc.get("output", "")
                if output:
                    out_line = str(output).split("\n")[0]
                    if len(out_line) > 60:
                        out_line = out_line[:60] + "..."
                    lines.append(f"  └─ ✓ {out_line}")
            cls = "tools-expanded" if self._tools_expanded else "tools-expanded hidden"
            yield Static("\n".join(lines), classes=cls)

            # Collapsed version
            from collections import Counter
            counts = Counter(tc.get("name", "") for tc in turn.tool_calls)
            parts = []
            for name, count in counts.items():
                if count > 1:
                    parts.append(f"{name} ×{count}")
                else:
                    parts.append(name)
            total = len(turn.tool_calls)
            cls = "tools-collapsed" if not self._tools_expanded else "tools-collapsed hidden"
            yield Static(
                f"🔧 {total} tool call{'s' if total > 1 else ''}: {', '.join(parts)} [Ctrl+D]",
                classes=cls,
            )

        # Thinking — both versions
        if turn.thinking:
            # Expanded
            cls = "thinking-expanded" if self._thinking_expanded else "thinking-expanded hidden"
            w = Static(turn.thinking, classes=cls)
            w.border_title = "💭 Thinking"
            yield w

            # Collapsed
            char_count = len(turn.thinking)
            cls = "thinking-collapsed" if not self._thinking_expanded else "thinking-collapsed hidden"
            yield Static(
                f"💭 Thinking ({char_count} chars) [Ctrl+T]",
                classes=cls,
            )

        # Response
        if turn.response:
            from textual.widgets import Markdown
            yield Markdown(turn.response, classes="response-text")

    def set_thinking_expanded(self, expanded: bool) -> None:
        """Toggle thinking visibility via CSS classes (no rebuild)."""
        self._thinking_expanded = expanded
        for w in self.query(".thinking-expanded"):
            w.set_class(not expanded, "hidden")
        for w in self.query(".thinking-collapsed"):
            w.set_class(expanded, "hidden")

    def set_tools_expanded(self, expanded: bool) -> None:
        """Toggle tools visibility via CSS classes (no rebuild)."""
        self._tools_expanded = expanded
        for w in self.query(".tools-expanded"):
            w.set_class(not expanded, "hidden")
        for w in self.query(".tools-collapsed"):
            w.set_class(expanded, "hidden")
        for widget in self.compose():
            self.mount(widget)


# ── Main App ─────────────────────────────────────────────────────────────────


class AlexApp(App):
    """Alex Agent TUI — chat interface with scrollable history."""

    TITLE = "Alex"
    SUB_TITLE = "Ctrl+T: toggle thinking | Ctrl+D: toggle tools | /clear | /quit"

    CSS = """
    #chat-view {
        height: 1fr;
        overflow-y: scroll;
        padding: 0 1;
    }
    #input-box {
        dock: bottom;
        height: 3;
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+t", "toggle_thinking", "Thinking", show=False),
        Binding("ctrl+d", "toggle_tools", "Tools", show=False),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, agent, **kwargs) -> None:
        super().__init__(**kwargs)
        self._agent = agent
        self._history = ChatHistory()  # New session by default
        self._thinking_expanded = False
        self._tools_expanded = False
        self._showing_session_list = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VerticalScroll(id="chat-view")
        yield Input(placeholder="Message Alex... (Ctrl+T: thinking, Ctrl+C: quit)", id="input-box")

    def on_mount(self) -> None:
        """Start fresh — no auto-restore."""
        self.query_one("#input-box", Input).focus()

    @on(Input.Submitted, "#input-box")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user input submission."""
        user_input = event.value.strip()
        event.input.clear()

        if not user_input:
            return

        cmd = user_input.lower()
        if cmd in ("/quit", "quit", "exit"):
            self.exit()
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

        # If session list is showing and user typed a number, select that session
        if self._showing_session_list:
            self._handle_session_selection(user_input)
            return

        # Show user message immediately
        chat_view = self.query_one("#chat-view", VerticalScroll)
        chat_view.mount(UserBubble(user_input))
        chat_view.scroll_end()

        # Start async response
        self._run_chat(user_input)

    @work(exclusive=True)
    async def _run_chat(self, user_input: str) -> None:
        """Run agent chat in background worker."""
        chat_view = self.query_one("#chat-view", VerticalScroll)

        # Show status
        status_widget = Static("  💭 Thinking...", id="status-line")
        chat_view.mount(status_widget)
        chat_view.scroll_end()

        collected = ""
        collected_thinking = ""
        tool_calls: list[dict] = []
        current_tool: dict | None = None

        try:
            async for event in self._agent.chat_stream(user_input):
                if event.type == "thinking":
                    collected_thinking += event.data
                    status_widget.update(f"  💭 Thinking... ({len(collected_thinking)} chars)")

                elif event.type == "token":
                    collected += event.data
                    # Show partial response
                    preview = collected[:80] + "..." if len(collected) > 80 else collected
                    status_widget.update(f"  ✍️ {preview}")

                elif event.type == "tool_start":
                    name = event.data.get("name", "")
                    args = event.data.get("input", {})
                    if not isinstance(args, dict):
                        args = {"input": str(args)}
                    current_tool = {"name": name, "args": args, "output": ""}
                    status_widget.update(f"  ◈ {name}...")

                elif event.type == "tool_end":
                    output_str = str(event.data.get("output", ""))
                    if current_tool:
                        current_tool["output"] = output_str
                        tool_calls.append(current_tool)
                        current_tool = None

                elif event.type == "done":
                    break

        except Exception as e:
            status_widget.update(f"  ✗ Error: {e}")
            return

        # Remove status
        status_widget.remove()

        # Build the turn
        turn = ChatTurn(
            user_input=user_input,
            response=collected,
            thinking=collected_thinking,
            tool_calls=tool_calls,
        )
        self._history.add(turn)

        # Render the Alex bubble
        bubble = AlexBubble(turn, thinking_expanded=self._thinking_expanded, tools_expanded=self._tools_expanded)
        chat_view.mount(bubble)
        chat_view.scroll_end()

    def _render_turn(self, turn: ChatTurn) -> None:
        """Render a full turn — used for history restore."""
        chat_view = self.query_one("#chat-view", VerticalScroll)
        chat_view.mount(UserBubble(turn.user_input))
        bubble = AlexBubble(turn, thinking_expanded=self._thinking_expanded, tools_expanded=self._tools_expanded)
        chat_view.mount(bubble)

    def action_toggle_thinking(self) -> None:
        """Toggle all thinking blocks expanded/collapsed."""
        self._thinking_expanded = not self._thinking_expanded
        for bubble in self.query(AlexBubble):
            bubble.set_thinking_expanded(self._thinking_expanded)

    def action_toggle_tools(self) -> None:
        """Toggle all tool call blocks expanded/collapsed."""
        self._tools_expanded = not self._tools_expanded
        for bubble in self.query(AlexBubble):
            bubble.set_tools_expanded(self._tools_expanded)

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

        lines = ["📋 Saved sessions (type number to resume, or anything else to cancel):", ""]
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
        status_widget = Static(f"  🔧 Merging skills... ({before_count} skills, this may take a moment)")
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
