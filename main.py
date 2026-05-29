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

from __future__ import annotations

import logging

from alex.agent import Agent, create_agent
from alex.app_logging import configure_logging
from alex.bus import AsyncEventBus
from alex.prompts import get_system_prompt
from alex.tools import (
    FileReadTracker,
    create_available_shell_tools,
    create_edit_tool,
    create_read_tool,
    create_write_tool,
    create_git_inspect_tool,
    create_glob_tool,
    create_grep_tool,
    create_time_tool,
    create_web_fetch_tool,
    create_web_search_tool,
    get_tool_hints,
)
from alex.tools.cron import create_cron_tool

logger = logging.getLogger(__name__)


def _build_agent(bus: AsyncEventBus | None = None) -> Agent:
    """Compose an Agent with the built-in toolset and any user plugins."""
    # One tracker shared across read / write / edit so the
    # read-before-edit invariant is enforced consistently.
    tracker = FileReadTracker()

    base_tools = [
        create_time_tool(),
        create_web_search_tool(),
        create_web_fetch_tool(),
        create_read_tool(tracker=tracker),
        create_write_tool(tracker=tracker),
        create_edit_tool(tracker=tracker),
        create_glob_tool(),
        create_grep_tool(),
        create_git_inspect_tool(),
        *create_available_shell_tools(),
    ]

    agent, plugin_results = create_agent(
        system_prompt=get_system_prompt(tool_hints=get_tool_hints()),
        max_iterations=5,
        tools=base_tools,
        event_bus=bus,
    )
    agent.register_tool(create_cron_tool(agent))

    for result in plugin_results:
        if result.error:
            logger.warning("plugin %s failed to load: %s", result.path.name, result.error)

    return agent


def main() -> None:
    """Entry point — launches the Textual TUI."""
    log_path = configure_logging()
    logger.info("Alex logging initialized at %s", log_path)
    bus = AsyncEventBus()
    agent = _build_agent(bus)
    from alex.tui import AlexApp
    app = AlexApp(agent, event_bus=bus)
    app.run()


if __name__ == "__main__":
    main()
