#!/usr/bin/env python3
"""Alex - An agent with web tools.

Usage:
    python main.py                    # Interactive TUI mode
    python main.py "search for ..."   # Single query mode
    python main.py --stream "..."     # Streaming mode (simple CLI)

Keyboard shortcuts (TUI mode):
    Ctrl+T    Toggle thinking expanded/collapsed
    Ctrl+C    Quit
"""

import asyncio
import sys

from alex.agent import Agent
from alex.callbacks import ToolDisplayCallback
from alex.display import (
    console,
    renderer,
    thinking_display,
    DisplayEvent,
    EventType,
)
from alex.prompts import get_system_prompt
from alex.tools import (
    create_time_tool,
    create_web_fetch_tool,
    create_web_search_tool,
    get_tool_hints,
)
from alex.tools.cron import create_cron_tool

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text


def create_agent() -> Agent:
    """Create and configure an agent with all available tools."""
    agent = Agent(
        system_prompt=get_system_prompt(tool_hints=get_tool_hints()),
        max_iterations=5,
        tools=[create_time_tool(), create_web_search_tool(), create_web_fetch_tool()],
        callbacks=[ToolDisplayCallback()],
    )
    agent.register_tool(create_cron_tool(agent))
    return agent


# ── Simple CLI modes (non-TUI) ──────────────────────────────────────────────


async def single_query(agent: Agent, query: str) -> None:
    """Run a single query and exit (no TUI)."""
    response = await agent.chat(query)

    if response.thinking:
        panel = Panel(
            Text(response.thinking.strip(), style="dim italic"),
            title="[dim yellow]💭 Thinking[/dim yellow]",
            title_align="left",
            border_style="dim yellow",
            width=min(console.width - 2, 88),
            padding=(0, 1),
        )
        console.print(panel)
        console.print()

    md = Markdown(str(response))
    console.print(md, width=min(console.width - 4, 88))
    console.print()


async def streaming_query(agent: Agent, query: str) -> None:
    """Run a single query with streaming output (no TUI)."""
    from rich.live import Live

    collected = ""
    collected_thinking = ""

    with Live(console=console, refresh_per_second=10, transient=True) as live:
        async for event in agent.chat_stream(query):
            if event.type == "thinking":
                collected_thinking += event.data
                output = Text()
                output.append("  💭 ", style="dim yellow")
                output.append(f"Thinking... ({len(collected_thinking)} chars)", style="dim yellow italic")
                live.update(output)
            elif event.type == "token":
                collected += event.data
                live.update(Text(collected))
            elif event.type == "done":
                break

    console.print()
    if collected_thinking:
        panel = Panel(
            Text(collected_thinking.strip(), style="dim italic"),
            title="[dim yellow]💭 Thinking[/dim yellow]",
            title_align="left",
            border_style="dim yellow",
            width=min(console.width - 2, 88),
            padding=(0, 1),
        )
        console.print(panel)
        console.print()
    if collected:
        md = Markdown(collected)
        console.print(md, width=min(console.width - 4, 88))
    console.print()


# ── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    """Entry point."""
    agent = create_agent()

    args = sys.argv[1:]
    if "--stream" in args:
        args.remove("--stream")
        query = " ".join(args) if args else input("Query: ")
        asyncio.run(streaming_query(agent, query))
    elif args:
        query = " ".join(args)
        asyncio.run(single_query(agent, query))
    else:
        # Interactive TUI mode
        from alex.tui import AlexApp
        app = AlexApp(agent)
        app.run()


if __name__ == "__main__":
    main()
