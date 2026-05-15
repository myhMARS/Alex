"""Time tool - get the current date and time."""

from datetime import datetime
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


class TimeInput(BaseModel):
    timezone: str = Field(
        default="local",
        description="Timezone name (e.g. 'Asia/Shanghai', 'US/Eastern', 'UTC') or 'local' for system local time",
    )


async def _get_current_time(timezone: str = "local") -> str:
    """Return the current date and time."""
    try:
        if timezone == "local":
            now = datetime.now().astimezone()
        else:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(timezone))

        return (
            f"Current time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"Day of week: {now.strftime('%A')}\n"
            f"ISO 8601: {now.isoformat()}"
        )
    except Exception as e:
        return f"Error getting current time: {type(e).__name__} - {e}"


def create_time_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_get_current_time,
        name="time",
        description=(
            "Get the current date and time. "
            "Returns the current datetime including day of week and ISO 8601 format. "
            "Use this when you need to know what time it is now, "
            "or when the user asks about the current date/time. "
            "Optionally accepts a timezone name like 'Asia/Shanghai', 'US/Eastern', or 'UTC'."
        ),
        args_schema=TimeInput,
    )
