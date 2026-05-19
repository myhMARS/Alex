"""Terminal display layer — event-driven rendering with Rich.

Architecture:
    Event Sources (callbacks, streaming) → DisplayEvent → EventQueue → Renderer (Rich)

The renderer runs as an async task, consuming events from the queue and
updating the terminal via Rich Console + Live for dynamic content.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

# ── Shared console instance ──────────────────────────────────────────────────

console = Console()

# ── Display Events ───────────────────────────────────────────────────────────


class EventType(Enum):
    """All display event types."""
    TOOL_START = auto()
    TOOL_END = auto()
    TOKEN = auto()
    THINKING = auto()
    RESPONSE_DONE = auto()
    STATUS = auto()
    ERROR = auto()


@dataclass
class DisplayEvent:
    """A single display event to be rendered."""
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


# ── Tool State Tracking ──────────────────────────────────────────────────────


@dataclass
class ToolCallState:
    """Tracks a single in-flight tool call."""
    id: str
    name: str
    args: dict
    start_time: float = field(default_factory=time.time)
    output: str | None = None
    elapsed: float = 0.0

    @property
    def is_done(self) -> bool:
        return self.output is not None


# ── Renderer ─────────────────────────────────────────────────────────────────


class Renderer:
    """Consumes DisplayEvents and renders to terminal using Rich.

    Supports:
    - Parallel tool calls with live spinner
    - Streaming token output (typewriter effect)
    - Clean separation between tool status and response text
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[DisplayEvent] = asyncio.Queue()
        self._active_tools: dict[str, ToolCallState] = {}
        self._active_order: list[str] = []
        self._completed_tools: list[ToolCallState] = []
        self._streaming_tokens: list[str] = []
        self._running = False

    @property
    def queue(self) -> asyncio.Queue[DisplayEvent]:
        return self._queue

    def emit(self, event: DisplayEvent) -> None:
        """Thread-safe event submission (non-async)."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # drop event if queue is full (shouldn't happen)

    async def emit_async(self, event: DisplayEvent) -> None:
        """Async event submission."""
        await self._queue.put(event)

    # ── Event processing ─────────────────────────────────────────────────

    def _process_event(self, event: DisplayEvent) -> None:
        """Process a single event, updating internal state."""
        if event.type == EventType.TOOL_START:
            tid = str(event.data.get("id") or "")
            name = event.data.get("name", "unknown")
            if not tid:
                tid = f"{name}:{time.monotonic_ns()}"
            self._active_tools[tid] = ToolCallState(
                id=tid,
                name=name,
                args=event.data.get("args", {}),
                start_time=event.timestamp,
            )
            self._active_order.append(tid)

        elif event.type == EventType.TOOL_END:
            output = str(event.data.get("output", ""))
            tid = str(event.data.get("id") or "")
            if tid and tid in self._active_tools:
                tool = self._active_tools.pop(tid)
                try:
                    self._active_order.remove(tid)
                except ValueError:
                    pass
            else:
                while self._active_order and self._active_order[0] not in self._active_tools:
                    self._active_order.pop(0)
                fallback_id = self._active_order.pop(0) if self._active_order else ""
                tool = self._active_tools.pop(fallback_id) if fallback_id and fallback_id in self._active_tools else None
            if tool:
                tool.output = output
                tool.elapsed = time.time() - tool.start_time
                self._completed_tools.append(tool)

        elif event.type == EventType.TOKEN:
            token = event.data.get("token", "")
            self._streaming_tokens.append(token)

        elif event.type == EventType.RESPONSE_DONE:
            pass  # handled in run loop

        elif event.type == EventType.ERROR:
            console.print(f"  [red]✗[/red] [dim]{event.data.get('message', 'Unknown error')}[/dim]")

        elif event.type == EventType.STATUS:
            console.print(f"  [dim]··· {event.data.get('message', '')}[/dim]")

    # ── Rendering helpers ────────────────────────────────────────────────

    def _render_tool_group(self) -> Text | None:
        """Render the current tool call state as a Rich renderable."""
        if not self._active_tools and not self._completed_tools:
            return None

        output = Text()

        # Render completed tools
        for tool in self._completed_tools:
            output.append("  ")
            output.append("◈ ", style="yellow")
            output.append(tool.name, style="bold magenta")
            output.append("\n")
            for k, v in tool.args.items():
                v_str = str(v)
                if len(v_str) > 76:
                    v_str = v_str[:76] + "..."
                output.append(f"  ├─ ", style="dim")
                output.append(f"{k:<12s} ", style="dim cyan")
                output.append(f"{v_str}\n", style="white")
            # Result line
            result_line = str(tool.output or "").split("\n")[0]
            if len(result_line) > 80:
                result_line = result_line[:80] + "..."
            output.append("  └─ ", style="dim")
            output.append("✓ ", style="green")
            output.append(f"{result_line}  ({tool.elapsed:.1f}s)\n", style="dim")
            output.append("\n")

        # Render active (in-flight) tools
        if self._active_tools:
            active_list = [self._active_tools[tid] for tid in self._active_order if tid in self._active_tools]
            if not active_list:
                active_list = list(self._active_tools.values())
            # Group by tool name for compact display
            names = [t.name for t in active_list]
            same_tool = len(set(names)) == 1
            count = len(active_list)

            if same_tool and count > 1:
                # Parallel same-tool: compact
                output.append("  ")
                output.append("◈ ", style="yellow")
                output.append(active_list[0].name, style="bold magenta")
                output.append(f" ×{count}", style="dim")
                output.append("  ")
                output.append("⟳ running...", style="dim yellow")
                output.append("\n")
                # Show differing args
                all_keys: list[str] = []
                for t in active_list:
                    for k in t.args:
                        if k not in all_keys:
                            all_keys.append(k)
                for k in all_keys:
                    values = [str(t.args.get(k, "")) for t in active_list]
                    unique = list(dict.fromkeys(values))
                    if len(unique) == 1:
                        v_str = unique[0]
                        if len(v_str) > 76:
                            v_str = v_str[:76] + "..."
                        output.append(f"  ├─ ", style="dim")
                        output.append(f"{k:<12s} ", style="dim cyan")
                        output.append(f"{v_str}\n", style="white")
                    else:
                        for i, v in enumerate(values, 1):
                            if len(v) > 68:
                                v = v[:68] + "..."
                            output.append(f"  ├─ ", style="dim")
                            output.append(f"{k:<12s} ", style="dim cyan")
                            output.append(f"[{i}] ", style="dim")
                            output.append(f"{v}\n", style="white")
            else:
                # Single or mixed tools
                for tool in active_list:
                    output.append("  ")
                    output.append("◈ ", style="yellow")
                    output.append(tool.name, style="bold magenta")
                    output.append("  ")
                    output.append("⟳ running...", style="dim yellow")
                    output.append("\n")
                    for k, v in tool.args.items():
                        v_str = str(v)
                        if len(v_str) > 76:
                            v_str = v_str[:76] + "..."
                        output.append(f"  ├─ ", style="dim")
                        output.append(f"{k:<12s} ", style="dim cyan")
                        output.append(f"{v_str}\n", style="white")

        return output if output.plain.strip() else None

    # ── Public rendering methods ─────────────────────────────────────────

    async def render_tool_calls(self) -> None:
        """Render tool calls with live updating (spinner while in-flight).

        Blocks until all active tool calls complete.
        """
        if not self._active_tools and self._queue.empty():
            return

        with Live(console=console, refresh_per_second=8, transient=True) as live:
            while self._active_tools or not self._queue.empty():
                # Drain available events
                try:
                    event = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                    self._process_event(event)
                except asyncio.TimeoutError:
                    pass

                # Update live display
                renderable = self._render_tool_group()
                if renderable:
                    live.update(renderable)

                # If no more active tools and queue is empty, we're done
                if not self._active_tools and self._queue.empty():
                    break

        # Print final completed state (non-transient)
        self._print_completed_tools()

    def _print_completed_tools(self) -> None:
        """Print completed tools as permanent output."""
        for tool in self._completed_tools:
            console.print()
            text = Text()
            text.append("  ")
            text.append("◈ ", style="yellow")
            text.append(tool.name, style="bold magenta")
            console.print(text)

            for k, v in tool.args.items():
                v_str = str(v)
                if len(v_str) > 76:
                    v_str = v_str[:76] + "..."
                line = Text()
                line.append(f"  ├─ ", style="dim")
                line.append(f"{k:<12s} ", style="dim cyan")
                line.append(v_str, style="white")
                console.print(line)

            result_line = str(tool.output or "").split("\n")[0]
            if len(result_line) > 80:
                result_line = result_line[:80] + "..."
            line = Text()
            line.append("  └─ ", style="dim")
            line.append("✓ ", style="green")
            line.append(f"{result_line}  ({tool.elapsed:.1f}s)", style="dim")
            console.print(line)

        self._completed_tools.clear()

    def print_streaming_response(self) -> None:
        """Print accumulated streaming tokens as final response."""
        if self._streaming_tokens:
            full_text = "".join(self._streaming_tokens)
            console.print()
            # Render as markdown for rich formatting
            md = Markdown(full_text)
            console.print(md, width=min(console.width - 4, 88))
            console.print()
            self._streaming_tokens.clear()

    def reset(self) -> None:
        """Reset state between conversation turns."""
        self._active_tools.clear()
        self._completed_tools.clear()
        self._streaming_tokens.clear()
        # Drain queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break


# ── Global renderer instance ─────────────────────────────────────────────────

renderer = Renderer()


# ── Convenience functions (backward-compatible API) ──────────────────────────


def tool_start(name: str, args: dict) -> None:
    """Register a tool call start event."""
    renderer.emit(DisplayEvent(
        type=EventType.TOOL_START,
        data={"name": name, "args": args},
    ))


def tool_end(output: str) -> None:
    """Register a tool call end event."""
    renderer.emit(DisplayEvent(
        type=EventType.TOOL_END,
        data={"output": output},
    ))


# ── Static display functions ─────────────────────────────────────────────────


def show_banner() -> None:
    """Display the welcome banner."""
    title = Text()
    title.append("Alex", style="bold cyan")
    title.append("  ·  ", style="dim")
    title.append("Agent", style="dim")

    subtitle = Text()
    subtitle.append("LangChain  ·  web_search  ·  web_fetch", style="dim")

    commands = Text()
    commands.append("/quit  /clear  /reflect", style="dim")

    panel = Panel(
        Group(title, subtitle, Text(), commands),
        border_style="green",
        width=min(console.width - 2, 60),
        padding=(0, 2),
    )
    console.print(panel)
    console.print()


def user_prompt(text: str) -> None:
    """Display user input."""
    line = Text()
    line.append("  ▸ ", style="bold cyan")
    line.append(text, style="bold")
    console.print(line)
    console.print()


def agent_response(text: str) -> None:
    """Display agent response as rendered markdown."""
    console.print()
    md = Markdown(text)
    console.print(md, width=min(console.width - 4, 88))
    console.print()


def status(msg: str) -> None:
    """Display a status message."""
    console.print(f"  [dim]··· {msg}[/dim]")
    console.print()


def divider() -> None:
    """Display a divider line."""
    w = min(console.width - 4, 60)
    console.print(f"  [dim]{'─' * w}[/dim]")
    console.print()


def error(msg: str) -> None:
    """Display an error message."""
    console.print(f"  [red]✗[/red] [dim]{msg}[/dim]")
    console.print()


# ── Thinking display ─────────────────────────────────────────────────────────


@dataclass
class ThinkingEntry:
    """A single thinking record from one conversation turn."""
    content: str
    turn: int  # 1-based turn number


class ThinkingDisplay:
    """Manages the display of LLM thinking/reasoning content with history.

    Stores all thinking content from the session. User can:
    - Toggle expanded/collapsed mode (affects all renders)
    - /thinking        — show last thinking
    - /thinking all    — show all history
    - /thinking N      — show thinking from turn N
    """

    def __init__(self) -> None:
        self._expanded: bool = False
        self._history: list[ThinkingEntry] = []
        self._current: str = ""  # accumulator for current streaming turn
        self._turn_counter: int = 0

    @property
    def expanded(self) -> bool:
        return self._expanded

    @expanded.setter
    def expanded(self, value: bool) -> None:
        self._expanded = value

    def toggle(self) -> None:
        """Toggle expanded/collapsed state."""
        self._expanded = not self._expanded

    @property
    def has_content(self) -> bool:
        """Whether the current turn has thinking content."""
        return bool(self._current.strip())

    @property
    def history_count(self) -> int:
        return len(self._history)

    def begin_turn(self) -> None:
        """Start a new turn — resets the current accumulator."""
        self._current = ""
        self._turn_counter += 1

    def append(self, chunk: str) -> None:
        """Append a chunk of thinking content for the current turn."""
        self._current += chunk

    def commit(self) -> None:
        """Commit the current turn's thinking to history."""
        if self._current.strip():
            self._history.append(ThinkingEntry(
                content=self._current,
                turn=self._turn_counter,
            ))

    def set_content(self, content: str) -> None:
        """Set thinking content directly (for non-streaming chat)."""
        self._turn_counter += 1
        self._current = content
        self.commit()

    def reset(self) -> None:
        """Alias for begin_turn (backward compat)."""
        self.begin_turn()

    def clear_history(self) -> None:
        """Clear all thinking history."""
        self._history.clear()
        self._current = ""
        self._turn_counter = 0

    # ── Rendering ────────────────────────────────────────────────────────

    def _render_entry(self, entry: ThinkingEntry, show_turn: bool = False) -> None:
        """Render a single thinking entry."""
        turn_label = f" (turn {entry.turn})" if show_turn else ""

        if self._expanded:
            content = entry.content.strip()
            panel = Panel(
                Text(content, style="dim italic"),
                title=f"[dim yellow]💭 Thinking{turn_label}[/dim yellow]",
                title_align="left",
                border_style="dim yellow",
                width=min(console.width - 2, 88),
                padding=(0, 1),
            )
            console.print(panel)
        else:
            char_count = len(entry.content)
            lines_count = entry.content.count("\n") + 1
            preview = entry.content.replace("\n", " ").strip()
            if len(preview) > 60:
                preview = preview[:60] + "..."
            line = Text()
            line.append(f"  💭{turn_label} ", style="dim yellow")
            line.append(f"({char_count} chars, {lines_count} lines) ", style="dim")
            line.append(preview, style="dim italic")
            console.print(line)

    def render(self) -> None:
        """Render the latest thinking entry."""
        if not self._history:
            return
        self._render_entry(self._history[-1])

    def render_last(self) -> None:
        """Render the most recent thinking (alias)."""
        self.render()

    def render_turn(self, turn: int) -> None:
        """Render thinking from a specific turn number."""
        for entry in self._history:
            if entry.turn == turn:
                self._render_entry(entry, show_turn=True)
                return
        console.print(f"  [dim]No thinking content for turn {turn}.[/dim]")

    def render_all(self) -> None:
        """Render all thinking history."""
        if not self._history:
            console.print("  [dim]No thinking history.[/dim]")
            return
        for entry in self._history:
            self._render_entry(entry, show_turn=True)
            console.print()

    def rerender(self) -> None:
        """Re-render last thinking with current mode. For /thinking command."""
        if not self._history:
            console.print("  [dim]No thinking content available.[/dim]")
            return
        self.render()

    def render_streaming(self) -> Text:
        """Render current thinking state for Live display."""
        if not self._current:
            return Text()

        output = Text()
        char_count = len(self._current)
        output.append("  💭 ", style="dim yellow")
        output.append(f"Thinking... ({char_count} chars)", style="dim yellow italic")
        output.append("\n")
        return output


# Global thinking display instance
thinking_display = ThinkingDisplay()
