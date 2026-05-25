"""TUI presenter — bubble widgets and turn rendering functions."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from alex.tui.view_models import ChatTurn


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


# ── Rendering helpers ────────────────────────────────────────────────────────


def render_turn(chat_view, turn: ChatTurn, *, thinking_expanded: bool = False, skills_expanded: bool = False) -> None:
    """Render a full turn — same path as live streaming.

    Creating AlexBubble(turn) directly skips finalize(), which means
    ToolBubble widgets are never mounted.  We instead create an empty
    streaming bubble and call finalize(turn) to reproduce the full
    live-chat rendering (skills, thinking, tool blocks, response).
    """
    if turn.kind != "cron":
        chat_view.mount(UserBubble(turn.user_input))
    bubble = AlexBubble(thinking_expanded=thinking_expanded, skills_expanded=skills_expanded)
    chat_view.mount(bubble)
    bubble.finalize(turn)
