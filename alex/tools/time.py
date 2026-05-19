"""Time tool - get the current date and time."""

from datetime import datetime, timedelta, timezone as dt_timezone
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


TOOL_HINT = "Use `time` to get the current the current date/time. Prefer timezone='local' unless the user specifies otherwise."


class TimeInput(BaseModel):
    timezone: str = Field(
        default="local",
        description="Timezone name (e.g. 'Asia/Shanghai', 'US/Eastern', 'UTC') or 'local' for system local time",
    )

_TZ_ALIASES: dict[str, str] = {
    "CHINA STANDARD TIME": "Asia/Shanghai",
    "BEIJING": "Asia/Shanghai",
    "SHANGHAI": "Asia/Shanghai",
    "UTC": "UTC",
    "GMT": "UTC",
    "Z": "UTC",
}


def _normalize_tz_key(tz: str) -> str:
    s = (tz or "").strip()
    if not s:
        return "local"
    if s.lower() == "local":
        return "local"
    key = s.upper()
    return _TZ_ALIASES.get(key, s)


async def _get_current_time(timezone: str = "local") -> str:
    """Return the current date and time."""
    try:
        tz_key = _normalize_tz_key(timezone)
        if tz_key == "local":
            now = datetime.now().astimezone()
        else:
            if tz_key == "UTC":
                now = datetime.now(dt_timezone.utc)
            else:
                try:
                    from zoneinfo import ZoneInfo
                    now = datetime.now(ZoneInfo(tz_key))
                except Exception:
                    if str(tz_key).lower() != "asia/shanghai":
                        raise
                    now = datetime.now(dt_timezone(timedelta(hours=8), name="CST"))

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
