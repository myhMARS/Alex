"""Tool executor — 简化版，只负责执行工具。

权限检查已移至 ToolsModule（通过 bus 事件驱动），
executor 只做纯粹的工具查找和调用。
"""

from __future__ import annotations

from typing import Any

from alex.kernel.dto.tool import ToolExecutionContext
from alex.tools.registry import ToolRegistry


class ToolExecutor:
    """执行注册在 ToolRegistry 中的工具。

    不再包含权限检查逻辑 — 权限确认由 ToolsModule 通过 bus 事件处理。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def execute(self, _ctx: ToolExecutionContext, name: str, args: dict[str, Any]) -> str:
        """查找并执行工具，返回字符串结果。"""
        tool = self._registry.get(name)
        if tool is None:
            return f"Error: tool '{name}' not found"

        try:
            result = await tool.invoke(args)
            return str(result)
        except Exception as e:
            return f"Error executing {name}: {type(e).__name__}: {e}"
