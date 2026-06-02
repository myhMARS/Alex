"""WebFetch tool - fetch and extract content from web pages."""

import re

from pydantic import BaseModel, Field

from alex.tools.models import AlexTool
from alex.tools.permissions import PERMISSION_NETWORK

# ``httpx`` and ``bs4`` are imported lazily inside the tool body so they
# don't sit on the startup critical path (they account for ~190ms of
# import time and are only needed when the tool is actually invoked).

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


TOOL_HINT = "Use `web_fetch` to read and extract the content of a specific web page URL."


class WebFetchInput(BaseModel):
    url: str = Field(description="The URL of the web page to fetch")
    max_length: int = Field(default=8000, description="Maximum character length of returned content (default: 8000)")


async def _web_fetch(url: str, max_length: int = 8000) -> str:
    """Fetch and extract readable content from a web page URL."""
    if not url:
        return "Error: URL is required."

    import httpx

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                url,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            response.raise_for_status()

        content = _extract_content(response.text, url)
        if len(content) > max_length:
            content = content[:max_length] + "\n\n[Content truncated...]"

        return content
    except httpx.HTTPStatusError as e:
        return f"HTTP error fetching {url}: {e.response.status_code}"
    except httpx.RequestError as e:
        return f"Request error fetching {url}: {type(e).__name__} - {e}"
    except Exception as e:
        return f"Unexpected error fetching {url}: {type(e).__name__} - {e}"


def _extract_content(html: str, url: str) -> str:
    """Extract readable text content from HTML."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "iframe"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title else "No title"

    main = soup.find("main") or soup.find("article") or soup.find(id=re.compile(r"content|main|article", re.I))
    if main:
        text = main.get_text(separator="\n", strip=True)
    else:
        body = soup.find("body")
        text = body.get_text(separator="\n", strip=True) if body else ""

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    cleaned = "\n".join(lines)

    return f"Title: {title}\nURL: {url}\n\n{cleaned}"


def create_web_fetch_tool() -> AlexTool:
    return AlexTool.from_function(
        coroutine=_web_fetch,
        name="web_fetch",
        description=(
            "Fetch and extract content from a web page URL. "
            "Returns the page title and cleaned text content. "
            "Use this when you need to read the contents of a specific web page."
        ),
        args_schema=WebFetchInput,
        metadata={"required_permission": PERMISSION_NETWORK},
    )
