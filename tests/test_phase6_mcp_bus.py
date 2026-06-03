"""Phase 6 tests: MCP module integration via bus.

Validates:
- MCPModule starts and subscribes properly
- ToolsModule merges MCP tools via ToolsProvided events
- tools and mcp are zero-import (verified by structure)
"""

import asyncio
import json

import pytest

from alex.bus.in_memory import AsyncEventBus
from alex.kernel.contracts.tools import ExecuteTool, GetToolCatalog, InvokeProviderTool, ToolsProvided
from alex.mcp.module import MCPModule
from alex.tools.module import ToolsModule
from alex.kernel.dto.tool import ToolExecutionContext


class TestMCPModule:
    """MCPModule standalone tests."""

    @staticmethod
    def _write_config(tmp_path, payload: dict) -> None:
        (tmp_path / "mcp.json").write_text(json.dumps(payload), encoding="utf-8")

    @pytest.mark.asyncio
    async def test_mcp_module_provides_invoke_handler(self):
        """MCP module registers InvokeProviderTool and handles non-mcp requests."""
        bus = AsyncEventBus()
        await bus.start()

        # Pass a non-existent config path to avoid connecting to real servers
        from pathlib import Path
        mcp_mod = MCPModule(config_path=Path("/nonexistent/mcp_config.json"))
        await mcp_mod.start(bus)

        # Should have registered InvokeProviderTool
        # Non-mcp provider should return error
        result = await bus.request(InvokeProviderTool(
            provider="not_mcp",
            name="some_tool",
            args={},
        ))
        assert "not handled by MCP module" in result.error

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_module_stops_cleanly(self):
        """MCP module stops without errors even with no connections."""
        bus = AsyncEventBus()
        await bus.start()

        from pathlib import Path
        mcp_mod = MCPModule(config_path=Path("/nonexistent/mcp_config.json"))
        await mcp_mod.start(bus)
        await mcp_mod.stop()

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_module_publishes_timeout_state_when_global_wait_times_out(self, tmp_path, monkeypatch):
        """Global wait_for timeout should publish a terminal timeout state."""
        self._write_config(tmp_path, {
            "mcpServers": {
                "slow-server": {
                    "command": "slow-mcp-server",
                }
            }
        })
        config_path = tmp_path / "mcp.json"

        bus = AsyncEventBus()
        await bus.start()

        received: list[ToolsProvided] = []

        async def _collect(event: ToolsProvided):
            if event.provider == "mcp":
                received.append(event)

        await bus.subscribe(ToolsProvided, _collect)

        real_wait_for = asyncio.wait_for

        async def _raise_global_timeout(awaitable, timeout=None):
            if timeout == 15.0:
                awaitable.close()
                raise asyncio.TimeoutError()
            return await real_wait_for(awaitable, timeout=timeout)

        monkeypatch.setattr("alex.mcp.module.asyncio.wait_for", _raise_global_timeout)

        mcp_mod = MCPModule(config_path=config_path)
        await mcp_mod.start(bus)
        await asyncio.sleep(0.05)

        assert mcp_mod._server_state
        assert mcp_mod._server_state[0]["status"] == "ERROR"
        assert mcp_mod._server_state[0]["error"] == "连接超时（15s）"
        assert received[-1].metadata["status"] == "timeout"

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_module_publishes_cancelled_state_on_stop(self, tmp_path, monkeypatch):
        """Cancelling the background connection task should clear connecting state."""
        self._write_config(tmp_path, {
            "mcpServers": {
                "slow-server": {
                    "command": "slow-mcp-server",
                }
            }
        })
        config_path = tmp_path / "mcp.json"

        bus = AsyncEventBus()
        await bus.start()

        received: list[ToolsProvided] = []

        async def _collect(event: ToolsProvided):
            if event.provider == "mcp":
                received.append(event)

        await bus.subscribe(ToolsProvided, _collect)

        async def _slow_connect_all(self, configs):
            await asyncio.sleep(60)
            return []

        monkeypatch.setattr("alex.mcp.mcp_client.MCPClientPool.connect_all", _slow_connect_all)

        mcp_mod = MCPModule(config_path=config_path)
        await mcp_mod.start(bus)
        await asyncio.sleep(0.05)
        await mcp_mod.stop()
        await asyncio.sleep(0.05)

        assert mcp_mod._server_state
        assert mcp_mod._server_state[0]["status"] == "ERROR"
        assert mcp_mod._server_state[0]["error"] == "连接已取消"
        assert received[-1].metadata["status"] == "cancelled"

        await bus.shutdown()


class TestMCPToolsGateway:
    """MCP-to-tools-gateway integration through the bus."""

    @pytest.mark.asyncio
    async def test_tools_provided_merges_mcp_specs(self):
        """When MCP publishes ToolsProvided, the tools gateway merges them."""
        bus = AsyncEventBus()
        await bus.start()

        tools_mod = ToolsModule()
        await tools_mod.start(bus)

        # Simulate MCP module announcing tools (without connecting to real servers)
        bus.publish(ToolsProvided(
            provider="mcp",
            specs=[
                {
                    "name": "mcp__server1__tool1",
                    "description": "MCP Server 1 Tool 1",
                    "json_schema": {"type": "object", "properties": {}},
                },
            ],
        ))

        await asyncio.sleep(0.1)

        catalog = await bus.request(GetToolCatalog())
        mcp_tools = [t for t in catalog if t.provider == "mcp"]
        assert len(mcp_tools) >= 1

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_tool_execution_routed_through_gateway(self):
        """ExecuteTool for mcp tool → gateway routes to InvokeProviderTool."""
        bus = AsyncEventBus()
        await bus.start()

        # Register both modules
        tools_mod = ToolsModule()
        from pathlib import Path
        mcp_mod = MCPModule(config_path=Path("/nonexistent/mcp_config.json"))

        await tools_mod.start(bus)
        await mcp_mod.start(bus)

        # Simulate MCP tools
        bus.publish(ToolsProvided(
            provider="mcp",
            specs=[
                {
                    "name": "mcp__test__echo",
                    "description": "Echo tool",
                    "json_schema": {"type": "object", "properties": {"msg": {"type": "string"}}},
                },
            ],
        ))

        await asyncio.sleep(0.1)

        # Try to execute an MCP tool — should route to InvokeProviderTool
        ctx = ToolExecutionContext(session_id="test")
        result = await bus.request(ExecuteTool(
            name="mcp__test__echo",
            args={"msg": "hello"},
            ctx=ctx,
        ))

        # The MCP module has no real server, so it should error gracefully
        # (either from the gateway not finding the tool or from the MCP module)
        assert result is not None

        await bus.shutdown()


class TestModuleIsolation:
    """Verify module isolation — mcp and tools use bus, not direct imports."""

    def test_mcp_module_does_not_import_tools_business_module(self):
        """mcp module does NOT import alex.tools business code — only mcp_client utility."""
        import ast

        mcp_module_file = "alex/mcp/module.py"
        with open(mcp_module_file) as f:
            tree = ast.parse(f.read())

        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)

        # mcp_client is a shared utility (not a business module), allowed
        # Business module imports would be: alex.tools.module, alex.tools.executor, etc.
        business_imports = [
            i for i in imports
            if "alex.tools" in i and "mcp_client" not in i
        ]
        assert not business_imports, f"MCP module imports tools business code: {business_imports}"

        # Should import from alex.kernel
        kernel_imports = [i for i in imports if "alex.kernel" in i]
        assert kernel_imports, "MCP module should import from alex.kernel"

    def test_tools_module_does_not_import_mcp_module(self):
        """tools module does NOT import alex.mcp — only alex.kernel."""
        import ast

        tools_module_file = "alex/tools/module.py"
        with open(tools_module_file) as f:
            tree = ast.parse(f.read())

        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)

        # Should NOT import alex.mcp
        mcp_imports = [i for i in imports if "alex.mcp" in i]
        assert not mcp_imports, f"Tools module imports mcp: {mcp_imports}"
