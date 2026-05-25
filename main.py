#!/usr/bin/env python3
"""Alex — an agent with web tools, interactive TUI mode.

Usage:
    python main.py

Keyboard shortcuts:
    Ctrl+T    Toggle thinking expanded/collapsed
    Ctrl+K    Toggle skill blocks
    Ctrl+G    Rate last response good
    Ctrl+B    Rate last response bad
    Ctrl+C    Quit
"""

from alex.agent import Agent
from alex.bus import AsyncEventBus
from alex.prompts import get_system_prompt
from alex.tools import (
    create_time_tool,
    create_web_fetch_tool,
    create_web_search_tool,
    get_tool_hints,
)
from alex.tools.cron import create_cron_tool


def create_agent(bus: AsyncEventBus | None = None) -> Agent:
    """Create and configure an agent with all available tools."""
    agent = Agent(
        system_prompt=get_system_prompt(tool_hints=get_tool_hints()),
        max_iterations=5,
        tools=[create_time_tool(), create_web_search_tool(), create_web_fetch_tool()],
        event_bus=bus,
    )
    agent.register_tool(create_cron_tool(agent))  # Agent satisfies CronScheduler protocol
    return agent


def main() -> None:
    """Entry point — launches the Textual TUI."""
    bus = AsyncEventBus()
    agent = create_agent(bus)
    from alex.tui import AlexApp
    app = AlexApp(agent, event_bus=bus)
    app.run()


if __name__ == "__main__":
    main()
