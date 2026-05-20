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
from alex.prompts import get_system_prompt
from alex.tools import (
    create_time_tool,
    create_web_fetch_tool,
    create_web_search_tool,
    get_tool_hints,
)
from alex.tools.cron import create_cron_tool


def create_agent() -> Agent:
    """Create and configure an agent with all available tools."""
    agent = Agent(
        system_prompt=get_system_prompt(tool_hints=get_tool_hints()),
        max_iterations=5,
        tools=[create_time_tool(), create_web_search_tool(), create_web_fetch_tool()],
    )
    agent.register_tool(create_cron_tool(agent))
    return agent


def main() -> None:
    """Entry point — launches the Textual TUI."""
    agent = create_agent()
    from alex.tui import AlexApp
    app = AlexApp(agent)
    app.run()


if __name__ == "__main__":
    main()
