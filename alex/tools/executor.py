"""Tool executor — runs a registered tool by name with arguments."""

from __future__ import annotations

from typing import Any

from alex.tools.permissions import (
    PermissionPolicy,
    build_approval_request,
    is_gated,
    required_permission,
)
from alex.tools.ports import ToolExecutionContext
from alex.tools.registry import ToolRegistry


class ToolExecutor:
    """Executes tools from a ToolRegistry, returning string results.

    Tools must be registered before execution.  The executor consults a
    :class:`PermissionPolicy` for tools that declare a required permission
    via ``metadata["required_permission"]``.

    When a tool has already been wrapped via
    :func:`alex.tools.permissions.gate_tool_with_policy` the executor
    skips the redundant policy check; the wrapped coroutine performs the
    same check itself.  This avoids double-prompting when the same tool
    is invoked from both the user-turn loop and the cron path.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        permissions: PermissionPolicy | None = None,
    ) -> None:
        self._registry = registry
        self._permissions = permissions or PermissionPolicy()

    @property
    def permissions(self) -> PermissionPolicy:
        return self._permissions

    def set_permissions(self, policy: PermissionPolicy) -> None:
        """Replace the active permission policy.

        Used by the host (TUI / API) to inject a confirm hook after the
        executor has been constructed.
        """
        self._permissions = policy

    async def execute(self, ctx: ToolExecutionContext, name: str, args: dict[str, Any]) -> str:
        tool = self._registry.get(name)
        if tool is None:
            return f"Error: tool '{name}' not found"

        if not is_gated(tool):
            perm = required_permission(tool)
            if perm:
                request = await build_approval_request(tool, perm, args)
                granted, reason = await self._permissions.check_request(request)
                if not granted:
                    return f"Error: tool '{name}' blocked: {reason}"

        try:
            result = await tool.invoke(args)
            return str(result)
        except Exception as e:
            return f"Error executing {name}: {type(e).__name__}: {e}"
