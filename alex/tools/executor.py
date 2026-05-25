"""Tool executor — runs a registered tool by name with arguments."""

from __future__ import annotations

from typing import Any

from alex.tools.ports import ToolExecutionContext
from alex.tools.registry import ToolRegistry


class ToolExecutor:
    """Executes tools from a ToolRegistry, returning string results.

    Tools must be registered before execution.  The executor is a thin
    wrapper around LangChain BaseTool.ainvoke() — it adds standardised
    error handling.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def execute(self, ctx: ToolExecutionContext, name: str, args: dict[str, Any]) -> str:
        tool = self._registry.get(name)
        if tool is None:
            return f"Error: tool '{name}' not found"
        try:
            result = await tool.ainvoke(args)
            return str(result)
        except Exception as e:
            return f"Error executing {name}: {type(e).__name__}: {e}"
