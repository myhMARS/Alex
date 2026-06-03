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

from alex.kernel.contracts.tools import GetMCPStatus, InvokeProviderTool, ToolsProvided
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
        self._server_state: list[dict[str, Any]] = []

    async def start(self, bus: Any) -> None:
        self._bus = bus
        # Provide handlers
        bus.provide(InvokeProviderTool, self._handle_invoke)
        bus.provide(GetMCPStatus, self._handle_get_status)

        # 同步加载配置，存储初始状态，发布初始事件
        configs: list = []
        try:
            from alex.mcp.mcp_client import load_mcp_config
            configs = load_mcp_config(self._config_path)
        except Exception:
            logger.warning("MCP config load failed", exc_info=True)
            self._server_state = []
            bus.publish(ToolsProvided(provider="mcp", specs=[],
                metadata={"status": "config_error", "servers": []}))
            return

        if not configs:
            self._server_state = []
            bus.publish(ToolsProvided(provider="mcp", specs=[],
                metadata={"status": "no_servers", "servers": []}))
            logger.info("No MCP servers configured")
            return

        servers = [_serialize_config(cfg) for cfg in configs]
        self._server_state = servers
        bus.publish(ToolsProvided(provider="mcp", specs=[],
            metadata={"status": "connecting", "servers": servers}))
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
        Uses an independent safety timer to guarantee status update even
        when ``asyncio.wait_for`` cannot cancel a stuck subprocess.
        """
        if not configs:
            return

        conn_start = asyncio.get_running_loop().time()
        logger.info("MCP: _connect_servers START, %d servers, timeout=15s", len(configs))

        # Safety timer: publish timeout status after 15 s no matter what
        safety_fired = False

        async def _safety_timeout() -> None:
            nonlocal safety_fired
            await asyncio.sleep(15.0)
            safety_fired = True
            elapsed = asyncio.get_running_loop().time() - conn_start
            logger.warning("MCP: safety timer fired after %.1fs", elapsed)
            servers = [_serialize_config(cfg) for cfg in configs]
            for s in servers:
                if s.get("status") == "connecting":
                    s["status"] = "ERROR"
                    s["error"] = "连接超时（15s）"
            self._server_state = servers
            if self._bus:
                self._bus.publish(ToolsProvided(provider="mcp", specs=[],
                    metadata={"status": "timeout", "servers": servers}))

        safety_task = asyncio.create_task(_safety_timeout())

        try:
            def _publish_terminal_state(*, status: str, error: str) -> None:
                servers = [_serialize_config(cfg) for cfg in configs]
                for s in servers:
                    if s.get("status") == "connecting":
                        s["status"] = "ERROR"
                        s["error"] = error
                self._server_state = servers
                if self._bus:
                    self._bus.publish(ToolsProvided(
                        provider="mcp",
                        specs=[],
                        metadata={"status": status, "servers": servers},
                    ))

            try:
                logger.info("MCP: importing MCPClientPool...")
                from alex.mcp.mcp_client import MCPClientPool
                logger.info("MCP: MCPClientPool imported, starting connect_all...")
            except Exception:
                logger.warning("MCP client not available", exc_info=True)
                _publish_terminal_state(status="error", error="MCP client 不可用")
                return

            self._pool = MCPClientPool()
            try:
                logger.info("MCP: calling connect_all with wait_for(timeout=15.0)...")
                connections = await asyncio.wait_for(
                    self._pool.connect_all(configs),
                    timeout=15.0,
                )
                elapsed = asyncio.get_running_loop().time() - conn_start
                logger.info("MCP: connect_all completed in %.1fs, %d connections", elapsed, len(connections))

                # Processing and publish — wrapped to catch any exception
                all_specs: list[dict[str, Any]] = []
                server_details: list[dict[str, Any]] = []
                logger.info("MCP: processing %d connections...", len(connections))
                for conn in connections:
                    detail = _serialize_connection(conn)
                    logger.info("MCP:   %s status=%s error=%s tools=%d",
                        conn.config.name, detail.get("status"), conn.error, len(conn.tools))
                    server_details.append(detail)
                    if conn.error:
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

                if safety_fired:
                    logger.warning("MCP: skipping publish — safety timer already fired")
                elif not self._bus:
                    logger.error("MCP: skipping publish — bus is None!")
                else:
                    tool_count = len(all_specs)
                    error_count = sum(1 for d in server_details if d.get("error"))
                    self._server_state = server_details
                    self._bus.publish(ToolsProvided(provider="mcp", specs=all_specs,
                        metadata={
                            "status": "connected",
                            "tool_count": tool_count,
                            "servers": server_details,
                        }))
                    logger.info("MCP: announced %d tools from %d servers (errors=%d)", tool_count, len(connections), error_count)
            except asyncio.TimeoutError:
                elapsed = asyncio.get_running_loop().time() - conn_start
                logger.warning("MCP server connection timed out after %.1fs (> 15s)", elapsed)
                if not safety_fired:
                    _publish_terminal_state(status="timeout", error="连接超时（15s）")
                return
            except asyncio.CancelledError:
                logger.warning("MCP: connection task cancelled")
                if not safety_fired:
                    _publish_terminal_state(status="cancelled", error="连接已取消")
                raise
            except Exception as exc:
                logger.warning("MCP server connection failed", exc_info=True)
                if not safety_fired:
                    _publish_terminal_state(status="error", error=f"{type(exc).__name__}: {exc}")
                return
        finally:
            elapsed = asyncio.get_running_loop().time() - conn_start
            logger.info("MCP: _connect_servers FINISHED after %.1fs (safety_fired=%s)", elapsed, safety_fired)
            # Cancel safety timer if connections completed normally
            if not safety_fired:
                safety_task.cancel()
                try:
                    await safety_task
                except asyncio.CancelledError:
                    pass

    # ── request handler ──────────────────────────────────────────────────

    async def _handle_get_status(self, req: GetMCPStatus) -> dict[str, Any]:
        """Return current MCP server state (for TUI initial load)."""
        return {"servers": self._server_state}

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
        "tool_count": 0,
        "tools": [],
        "error": None,
    }


def _serialize_connection(conn: Any) -> dict[str, Any]:
    """Serialize an ``MCPConnection`` to a plain dict for bus metadata."""
    cfg = conn.config
    conn_tools = getattr(conn, "tools", []) or []
    tools_detail: list[dict[str, str]] = []
    for t in conn_tools:
        tools_detail.append({
            "name": getattr(t, "name", ""),
            "description": getattr(t, "description", ""),
        })
    return {
        "name": getattr(cfg, "name", ""),
        "transport": getattr(cfg, "transport", "stdio"),
        "command": getattr(cfg, "command", ""),
        "url": getattr(cfg, "url", ""),
        "enabled": getattr(cfg, "enabled", True),
        "status": "ERROR" if conn.error else "CONNECTED",
        "tool_count": len(conn_tools),
        "tools": tools_detail,
        "error": conn.error or None,
    }
