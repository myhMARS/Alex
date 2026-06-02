"""TUI presenter — bubble widgets and turn rendering functions."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from alex.tui.markdown import render_response
from alex.tui.tool_display import extract_read_path, format_read_display_path, is_read_tool_name, read_output_paths
from alex.tui.view_models import ChatTurn

_TOOL_OUTPUT_PREVIEW_LINES = 3
_TOOL_OUTPUT_PREVIEW_WIDTH = 96
_READ_OUTPUT_PREVIEW_LINES = 3


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
    SystemBubble.error {
        color: $error;
        text-style: bold;
    }
    """

    def __init__(self, text: str, *, is_error: bool = False) -> None:
        super().__init__(text)
        if is_error:
            self.add_class("error")


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

    def __init__(self, name: str, args: dict, *, output_expanded: bool = False):
        super().__init__()
        self.border_title = f"\U0001f527 {name}"
        self._name = name
        self._args = args
        self._output_expanded = output_expanded
        self._summary_widget: Static | None = None
        self._full_widget: Static | None = None

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
        full_output = str(output)
        if is_read_tool_name(self._name):
            read_paths = read_output_paths(full_output)
            count = len(read_paths)
            self.border_title = f"\U0001f527 read ({count} file{'s' if count != 1 else ''})"
            summary_output = _build_read_output_summary(read_paths)
            full_output = "\n".join(format_read_display_path(path) for path in read_paths)
        else:
            self.border_title = f"\U0001f527 {self._name}"
            summary_output = _build_tool_output_summary(full_output)
        if self._summary_widget is None:
            self._summary_widget = Static(summary_output, id="tool-output-summary")
            self.mount(self._summary_widget)
        else:
            self._summary_widget.update(summary_output)
        if self._full_widget is None:
            self._full_widget = Static(full_output, id="tool-output-full")
            self.mount(self._full_widget)
        else:
            self._full_widget.update(full_output)
        self.set_output_expanded(self._output_expanded)

    def set_output_expanded(self, expanded: bool) -> None:
        self._output_expanded = expanded
        if self._summary_widget is not None:
            self._summary_widget.set_class(expanded, "hidden")
        if self._full_widget is not None:
            self._full_widget.set_class(not expanded, "hidden")


def _truncate_middle(text: str, limit: int = _TOOL_OUTPUT_PREVIEW_WIDTH) -> str:
    if len(text) <= limit:
        return text
    keep = max(8, (limit - 3) // 2)
    return f"{text[:keep]}...{text[-keep:]}"


def _build_tool_output_summary(output: str) -> str:
    lines = str(output).splitlines() or [str(output)]
    if len(lines) == 1:
        return f"└─ {_truncate_middle(lines[0], 120)}"

    preview = [_truncate_middle(line) for line in lines[:_TOOL_OUTPUT_PREVIEW_LINES]]
    rendered = [f"└─ {preview[0]}"]
    rendered.extend(f"   {line}" for line in preview[1:])
    remaining = len(lines) - len(preview)
    if remaining > 0:
        rendered.append(f"   ... ({remaining} more lines) [Ctrl+O]")
    else:
        rendered[-1] = f"{rendered[-1]} [Ctrl+O]"
    return "\n".join(rendered)


def _build_read_output_summary(paths: list[str]) -> str:
    count = len(paths)
    if count == 0:
        return "└─ No files read"
    preview = [format_read_display_path(path) for path in paths[:_READ_OUTPUT_PREVIEW_LINES]]
    rendered = [f"└─ Read {count} file{'s' if count != 1 else ''}"]
    rendered.extend(f"   • {_truncate_middle(path, 88)}" for path in preview)
    remaining = count - len(preview)
    if remaining > 0:
        rendered.append(f"   ... ({remaining} more files) [Ctrl+O]")
    else:
        rendered[-1] = f"{rendered[-1]} [Ctrl+O]"
    return "\n".join(rendered)


def _coalesce_tool_calls(tool_calls: list[dict]) -> list[dict]:
    display_calls: list[dict] = []
    read_group: dict | None = None
    read_paths: list[str] = []

    for tc in tool_calls:
        name = str(tc.get("name") or "")
        if not is_read_tool_name(name):
            display_calls.append(tc)
            continue

        path = extract_read_path(tc)
        if path not in read_paths:
            read_paths.append(path)

        if read_group is None:
            read_group = {
                "name": "read",
                "args": {},
                "output": "",
                "prefix": tc.get("prefix", ""),
            }
            display_calls.append(read_group)

    if read_group is not None:
        read_group["output"] = "\n".join(read_paths)

    return display_calls


def _mount_tool(container: Vertical, tc: dict, *, output_expanded: bool = False) -> ToolBubble:
    """Mount a prefix widget (if any) and a ToolBubble into *container*."""
    prefix = tc.get("prefix", "")
    if prefix:
        container.mount(Static(render_response(prefix), classes="response-prefix"))
    name = tc.get("name", "")
    args = tc.get("args", {})
    tb = ToolBubble(name, args, output_expanded=output_expanded)
    container.mount(tb)
    output = tc.get("output", "")
    if output:
        tb.set_done(output)
    return tb


class AlexBubble(Vertical):
    """One bubble per agent response — renders thinking, skills, and streamed text."""

    def __init__(
        self,
        turn: ChatTurn | None = None,
        thinking_expanded: bool = False,
        skills_expanded: bool = False,
        tool_output_expanded: bool = False,
    ) -> None:
        super().__init__()
        self._turn = turn or ChatTurn(user_input="", response="", thinking="", tool_calls=[], skills=[])
        self._thinking_expanded = thinking_expanded
        self._skills_expanded = skills_expanded
        self._tool_output_expanded = tool_output_expanded
        self.border_title = "Alex"
        self._finalized = turn is not None
        self._current_response: Static | None = None
        self._current_thinking_expanded: Static | None = None
        self._current_thinking_collapsed: Static | None = None

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
            widgets.append(Static(render_response(turn.response), classes="response-text"))

        return widgets

    def set_response(self, text: str) -> None:
        """Stream response text into the current response section."""
        self._turn.response = text
        if self._current_response:
            self._current_response.update(text)

    def set_thinking(self, text: str) -> None:
        """Stream thinking text into live widgets before finalization."""
        self._turn.thinking = text
        if not text:
            return
        if self._current_thinking_expanded is None or self._current_thinking_collapsed is None:
            if self._current_response is not None:
                self._current_response.remove()
                self._current_response = None
            expanded_cls = "thinking-expanded" if self._thinking_expanded else "thinking-expanded hidden"
            collapsed_cls = "thinking-collapsed" if not self._thinking_expanded else "thinking-collapsed hidden"
            self._current_thinking_expanded = Static(text, classes=expanded_cls)
            self._current_thinking_expanded.border_title = "\U0001f4ad Thinking"
            self._current_thinking_collapsed = Static(
                f"\U0001f4ad Thinking ({len(text)} chars) [Ctrl+T]",
                classes=collapsed_cls,
            )
            self.mount(self._current_thinking_expanded)
            self.mount(self._current_thinking_collapsed)
            self._current_response = Static(self._turn.response, classes="response-text")
            self.mount(self._current_response)
            return
        self._current_thinking_expanded.update(text)
        self._current_thinking_collapsed.update(f"\U0001f4ad Thinking ({len(text)} chars) [Ctrl+T]")

    def insert_tool(self, name: str, args: dict) -> ToolBubble:
        """Insert a ToolBubble and keep assistant text below active tool output."""
        if self._current_response is not None:
            if self._turn.response:
                self.mount(Static(render_response(self._turn.response), classes="response-prefix"))
            self._current_response.remove()
            self._current_response = None
        tb = ToolBubble(name, args, output_expanded=self._tool_output_expanded)
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
        if turn.is_error:
            self.add_class("error")
            self.border_title = "Alex  ❌ Error"
        else:
            self.remove_class("error")
            self.border_title = "Alex"
        display_tool_calls = _coalesce_tool_calls(turn.tool_calls)
        for widget in self._build_sections():
            if "response-text" in widget.classes and display_tool_calls:
                for tc in display_tool_calls:
                    _mount_tool(self, tc, output_expanded=self._tool_output_expanded)
            self.mount(widget)
        if display_tool_calls and not turn.response:
            for tc in display_tool_calls:
                _mount_tool(self, tc, output_expanded=self._tool_output_expanded)

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

    def set_tool_output_expanded(self, expanded: bool) -> None:
        """Toggle tool output between summary and full content."""
        self._tool_output_expanded = expanded
        for bubble in self.query(ToolBubble):
            bubble.set_output_expanded(expanded)


# ── Rendering helpers ────────────────────────────────────────────────────────


def render_turn(
    chat_view,
    turn: ChatTurn,
    *,
    thinking_expanded: bool = False,
    skills_expanded: bool = False,
    tool_output_expanded: bool = False,
) -> None:
    """Render a full turn — same path as live streaming.

    Creating AlexBubble(turn) directly skips finalize(), which means
    ToolBubble widgets are never mounted.  We instead create an empty
    streaming bubble and call finalize(turn) to reproduce the full
    live-chat rendering (skills, thinking, tool blocks, response).
    """
    if turn.kind != "cron":
        chat_view.mount(UserBubble(turn.user_input))
    bubble = AlexBubble(
        thinking_expanded=thinking_expanded,
        skills_expanded=skills_expanded,
        tool_output_expanded=tool_output_expanded,
    )
    chat_view.mount(bubble)
    bubble.finalize(turn)
