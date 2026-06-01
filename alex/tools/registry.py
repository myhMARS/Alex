"""Tool registry — manages tool registration and lookup."""

from __future__ import annotations

from alex.tools.models import AlexTool


class ToolRegistry:
    """In-memory tool registry with register / unregister / get / list."""

    def __init__(self) -> None:
        self._tools: dict[str, AlexTool] = {}

    def register(self, tool: AlexTool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> AlexTool | None:
        return self._tools.get(name)

    def list(self) -> list[AlexTool]:
        return list(self._tools.values())
