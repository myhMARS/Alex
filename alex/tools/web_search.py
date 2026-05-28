"""WebSearch tool - search the web using DuckDuckGo."""

import asyncio

from ddgs import DDGS
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from alex.tools.permissions import PERMISSION_NETWORK


TOOL_HINT = "Use `web_search` to search the web for current information, facts, or answers beyond your knowledge cutoff."


class WebSearchInput(BaseModel):
    query: str = Field(description="The search query string")
    max_results: int = Field(default=5, description="Maximum number of results to return (max: 15)")


def _do_search(query: str, max_results: int) -> list[dict]:
    """Synchronous search — runs in a thread to avoid blocking the event loop."""
    return list(DDGS().text(query, max_results=max_results))


async def _web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo and return formatted results."""
    max_results = min(max_results, 15)

    if not query:
        return "Error: Search query is required."

    try:
        results = await asyncio.to_thread(_do_search, query, max_results)

        if not results:
            return f"No results found for query: '{query}'"

        lines = [f"Search results for: '{query}'\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "No title")
            href = r.get("href", "")
            body = r.get("body", "No description")
            lines.append(f"{i}. {title}")
            lines.append(f"   URL: {href}")
            lines.append(f"   {body}")
            lines.append("")

        return "\n".join(lines).strip()
    except Exception as e:
        return f"Search error for '{query}': {type(e).__name__} - {e}"


def create_web_search_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_web_search,
        name="web_search",
        description=(
            "Search the web for information using DuckDuckGo. "
            "Returns a list of search results with titles, snippets, and URLs. "
            "Use this when you need to find information on the web or answer questions "
            "that require up-to-date knowledge."
        ),
        args_schema=WebSearchInput,
        metadata={"required_permission": PERMISSION_NETWORK},
    )
