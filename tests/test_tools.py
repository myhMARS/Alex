"""Core tests for web tools."""

import pytest
pytest.importorskip("langchain_core")
from langchain_core.tools import StructuredTool

from alex.tools.web_fetch import WebFetchInput, _web_fetch, create_web_fetch_tool
from alex.tools.web_search import WebSearchInput, _web_search, create_web_search_tool


class TestWebSearch:
    def test_metadata(self):
        tool = create_web_search_tool()
        assert isinstance(tool, StructuredTool)
        assert tool.name == "web_search"
        assert "search" in tool.description.lower()
        assert tool.args_schema is WebSearchInput

    def test_args_schema_fields(self):
        fields = WebSearchInput.model_fields
        assert "query" in fields
        assert fields["query"].annotation is str
        assert "max_results" in fields
        assert fields["max_results"].default == 5

    @pytest.mark.asyncio
    async def test_missing_query(self):
        result = await _web_search(query="")
        assert "error" in result.lower()


class TestWebFetch:
    def test_metadata(self):
        tool = create_web_fetch_tool()
        assert isinstance(tool, StructuredTool)
        assert tool.name == "web_fetch"
        assert "fetch" in tool.description.lower()
        assert tool.args_schema is WebFetchInput

    def test_args_schema_fields(self):
        fields = WebFetchInput.model_fields
        assert "url" in fields
        assert fields["url"].annotation is str
        assert "max_length" in fields
        assert fields["max_length"].default == 8000

    @pytest.mark.asyncio
    async def test_missing_url(self):
        result = await _web_fetch(url="")
        assert "error" in result.lower()
