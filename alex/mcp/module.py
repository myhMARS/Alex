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

        # 同步加载配置并发布初始状态，让 TUI 及时展示
        configs: list = []
        try:
            from alex.mcp.mcp_client import load_mcp_config
            configs = load_mcp_config(self._config_path)
        except Exception:
            bus.publish(ToolsProvided(provider="mcp", specs=[],
                metadata={"status": "config_error", "message": "配置读取失败"}))
            logger.warning("MCP config load failed", exc_info=True)
            return

        if not configs:
            bus.publish(ToolsProvided(provider="mcp", specs=[],
                metadata={"status": "no_servers", "message": "未发现 MCP server 配置"}))
            logger.info("No MCP servers configured")
            return

        bus.publish(ToolsProvided(provider="mcp", specs=[],
            metadata={
                "status": "connecting", "message": "连接中（后台）",
                "servers": [_serialize_config(cfg) for cfg in configs],
            }))
        logger.info("MCP: connecting to %d servers in background", len(configs))

        # Connect to MCP servers in the background — never block startup
        self._connect_task = asyncio.create_task(self._connect_servers(configs))

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

    async def _connect_servers(self, configs: list) -> None:
        """Connect to *configs* (already loaded) and announce tools.

        Runs as a background task — a slow server never blocks the TUI.
        Each server gets a 15 s overall timeout.
        """
        if not configs:
            return

        try:
            from alex.mcp.mcp_client import MCPClientPool
        except Exception:
            logger.warning("MCP client not available", exc_info=True)
            if self._bus:
                self._bus.publish(ToolsProvided(provider="mcp", specs=[],
                    metadata={"status": "error", "message": "MCP client 不可用"}))
            return

        self._pool = MCPClientPool()
        try:
            connections = await asyncio.wait_for(
                self._pool.connect_all(configs),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.warning("MCP server connection timed out (15 s)")
            if self._bus:
                self._bus.publish(ToolsProvided(provider="mcp", specs=[],
                    metadata={"status": "timeout", "message": "连接超时（15s）"}))
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("MCP server connection failed", exc_info=True)
            if self._bus:
                self._bus.publish(ToolsProvided(provider="mcp", specs=[],
                    metadata={"status": "error", "message": "连接失败"}))
            return

        all_specs: list[dict[str, Any]] = []
        server_details: list[dict[str, Any]] = []
        for conn in connections:
            detail = _serialize_connection(conn)
            server_details.append(detail)
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

        if self._bus:
            tool_count = len(all_specs)
            self._bus.publish(ToolsProvided(provider="mcp", specs=all_specs,
                metadata={
                    "status": "connected",
                    "message": f"已连接，注册 {tool_count} 个工具",
                    "tool_count": tool_count,
                    "servers": server_details,
                }))
            logger.info("MCP: announced %d tools from %d servers", tool_count, len(connections))

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


# ── serialization helpers (no alex.tui dependency) ──────────────────────────


def _serialize_config(cfg: Any) -> dict[str, Any]:
    """Serialize an ``MCPServerConfig`` to a plain dict for bus metadata."""
    return {
        "name": getattr(cfg, "name", ""),
        "transport": getattr(cfg, "transport", "stdio"),
        "command": getattr(cfg, "command", ""),
        "url": getattr(cfg, "url", ""),
        "enabled": getattr(cfg, "enabled", True),
        "status": "connecting",
        "tool_count": None,
        "error": None,
    }


def _serialize_connection(conn: Any) -> dict[str, Any]:
    """Serialize an ``MCPConnection`` to a plain dict for bus metadata."""
    cfg = conn.config
    return {
        "name": getattr(cfg, "name", ""),
        "transport": getattr(cfg, "transport", "stdio"),
        "command": getattr(cfg, "command", ""),
        "url": getattr(cfg, "url", ""),
        "enabled": getattr(cfg, "enabled", True),
        "status": "ERROR" if conn.error else "CONNECTED",
        "tool_count": len(getattr(conn, "tools", []) or []),
        "error": conn.error or None,
    }
