"""Tool registry — manages tool registration and lookup."""

from __future__ import annotations

from langchain_core.tools import BaseTool as LCBaseTool


class ToolRegistry:
    """In-memory tool registry with register / unregister / get / list."""

    def __init__(self) -> None:
        self._tools: dict[str, LCBaseTool] = {}

    def register(self, tool: LCBaseTool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> LCBaseTool | None:
        return self._tools.get(name)

    def list(self) -> list[LCBaseTool]:
        return list(self._tools.values())
