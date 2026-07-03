import pytest

from alex.tools.time import _get_current_time


@pytest.mark.asyncio
async def test_time_tool_accepts_utc():
    out = await _get_current_time("UTC")
    assert "Error getting current time" not in out
    assert "ISO 8601:" in out
