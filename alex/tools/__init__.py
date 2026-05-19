"""Tool implementations for the Alex agent."""

from alex.tools.cron import TOOL_HINT as CRON_HINT
from alex.tools.time import TimeInput, create_time_tool, TOOL_HINT as TIME_HINT
from alex.tools.web_fetch import WebFetchInput, create_web_fetch_tool, TOOL_HINT as WEB_FETCH_HINT
from alex.tools.web_search import WebSearchInput, create_web_search_tool, TOOL_HINT as WEB_SEARCH_HINT

TOOL_HINTS = [TIME_HINT, WEB_SEARCH_HINT, WEB_FETCH_HINT, CRON_HINT]


def get_tool_hints() -> str:
    """Collect usage hints from all registered tool modules."""
    return "\n".join(f"- {h}" for h in TOOL_HINTS)


__all__ = [
    "TimeInput",
    "WebFetchInput",
    "WebSearchInput",
    "create_time_tool",
    "create_web_fetch_tool",
    "create_web_search_tool",
    "TOOL_HINTS",
    "get_tool_hints",
]
