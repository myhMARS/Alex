"""MCPModule — manages MCP server connections and exposes tools via bus.

Phase 6: Bridges external MCP servers into Alex.  Server connections run
in a **background task** so they never block startup.  Tools are announced
via ``ToolsProvided`` as each server connects.

Also responds to ``InvokeProviderTool`` for tool execution routed
through the tools gateway.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from alex.kernel.contracts.tools import InvokeProviderTool, ToolsProvided
from alex.kernel.dto.tool import ToolResult

logger = logging.getLogger(__name__)


class MCPModule:
    """Pluggable MCP module — connects to MCP servers and provides tools.

    Server connections run in the background so a slow / unreachable
    MCP server never blocks the TUI from appearing.
    """

    name = "mcp"
    dependencies: list[str] = ["tools"]

    def __init__(self, config_path: Any = None) -> None:
        self._config_path = config_path
        self._bus: Any = None
        self._pool: Any = None
        self._tools_by_name: dict[str, Any] = {}
        self._connect_task: asyncio.Task | None = None

    async def start(self, bus: Any) -> None:
        self._bus = bus
        # Provide handler for tool invocation (routed through tools gateway)
        bus.provide(InvokeProviderTool, self._handle_invoke)

        # Connect to MCP servers in the background — never block startup
        self._connect_task = asyncio.create_task(self._connect_servers())

        logger.info("MCPModule started (provides InvokeProviderTool, connecting in background)")

    async def stop(self) -> None:
        if self._connect_task is not None:
            self._connect_task.cancel()
            try:
                await self._connect_task
            except (asyncio.CancelledError, Exception):
                pass
            self._connect_task = None
        if self._pool is not None:
            try:
                await self._pool.aclose()
            except Exception:
                pass
            self._pool = None
        self._tools_by_name.clear()
        self._bus = None

    # ── server connection ────────────────────────────────────────────────

    async def _connect_servers(self) -> None:
        """Connect to configured MCP servers and announce tools.

        Runs as a background task — a slow server never blocks the TUI.
        Each server gets a 10 s timeout.
        """
        try:
            from alex.mcp.mcp_client import load_mcp_config, MCPClientPool
        except Exception:
            logger.warning("MCP client not available", exc_info=True)
            return

        try:
            configs = load_mcp_config(self._config_path)
        except Exception:
            logger.warning("Failed to load MCP config", exc_info=True)
            return

        if not configs:
            logger.info("No MCP servers configured")
            return

        self._pool = MCPClientPool()
        try:
            connections = await asyncio.wait_for(
                self._pool.connect_all(configs),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.warning("MCP server connection timed out (15 s)")
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("MCP server connection failed", exc_info=True)
            return

        all_specs: list[dict[str, Any]] = []
        for conn in connections:
            if conn.error:
                logger.warning(
                    "MCP server '%s' error: %s", conn.config.name, conn.error,
                )
                continue
            for tool in conn.tools:
                self._tools_by_name[tool.name] = tool
                all_specs.append({
                    "name": tool.name,
                    "description": tool.description,
                    "json_schema": tool.parameters,
                    "provider": "mcp",
                    "metadata": tool.metadata or {},
                })

        if all_specs and self._bus:
            self._bus.publish(ToolsProvided(provider="mcp", specs=all_specs))
            logger.info("MCP: announced %d tools from %d servers", len(all_specs), len(connections))

    # ── request handler ──────────────────────────────────────────────────

    async def _handle_invoke(self, req: InvokeProviderTool) -> ToolResult:
        """Execute a tool via the MCP provider."""
        import uuid as _uuid

        if req.provider != "mcp":
            return ToolResult(
                name=req.name,
                error=f"Provider '{req.provider}' not handled by MCP module",
                run_id=_uuid.uuid4().hex[:12],
            )

        tool = self._tools_by_name.get(req.name)
        if tool is None:
            return ToolResult(
                name=req.name,
                error=f"MCP tool '{req.name}' not found",
                run_id=_uuid.uuid4().hex[:12],
            )

        run_id = _uuid.uuid4().hex[:12]
        try:
            output = await tool.invoke(req.args)
            return ToolResult(name=req.name, output=str(output), run_id=run_id)
        except Exception as e:
            return ToolResult(name=req.name, error=str(e), run_id=run_id)
