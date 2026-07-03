"""Phase 5 tests: tools operations through bus request/reply.

Validates:
- ToolsModule provides GetToolCatalog and ExecuteTool via bus
- TurnProcessor can use bus-based tool catalog and execution
- ToolsProvided events are merged into catalog
"""

import asyncio

import pytest

from alex.bus.in_memory import AsyncEventBus
from alex.kernel.contracts.tools import ExecuteTool, GetToolCatalog, ToolsProvided
from alex.tools.models import AlexTool
from alex.tools.module import ToolsModule
from alex.kernel.dto.tool import ToolExecutionContext


class TestToolsBusIntegration:
    """Tools gateway operations through the bus."""

    @pytest.mark.asyncio
    async def test_catalog_includes_registered_tools(self):
        bus = AsyncEventBus()
        await bus.start()

        tools_mod = ToolsModule()

        async def sample_tool(x: str = "") -> str:
            return f"result: {x}"

        tool = AlexTool.from_function(
            name="sample",
            description="A sample tool",
            coroutine=sample_tool,
        )
        tools_mod.register_tool(tool)
        await tools_mod.start(bus)

        catalog = await bus.request(GetToolCatalog())
        names = [t.name for t in catalog]
        assert "sample" in names

        sample = next(t for t in catalog if t.name == "sample")
        assert sample.provider == "builtin"
        assert sample.description == "A sample tool"

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_execute_tool_through_bus(self):
        bus = AsyncEventBus()
        await bus.start()

        tools_mod = ToolsModule()

        async def greet(name: str = "World") -> str:
            return f"Hello, {name}!"

        tool = AlexTool.from_function(
            name="greet",
            description="Greet someone",
            coroutine=greet,
        )
        tools_mod.register_tool(tool)
        await tools_mod.start(bus)

        ctx = ToolExecutionContext(session_id="test", source="user")
        result = await bus.request(ExecuteTool(
            name="greet",
            args={"name": "BusUser"},
            ctx=ctx,
        ))

        assert result.ok is True
        assert "Hello, BusUser!" in result.output

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_provider_tools_announcement(self):
        """ToolsProvided events from providers are merged into catalog."""
        bus = AsyncEventBus()
        await bus.start()

        tools_mod = ToolsModule()
        await tools_mod.start(bus)

        # Simulate mcp/server announcing tools
        bus.publish(ToolsProvided(
            provider="mcp",
            specs=[
                {
                    "name": "mcp_tool_1",
                    "description": "MCP tool 1",
                    "json_schema": {"type": "object", "properties": {}},
                },
                {
                    "name": "mcp_tool_2",
                    "description": "MCP tool 2",
                    "json_schema": {"type": "object", "properties": {}},
                },
            ],
        ))

        # Wait for event dispatch (poll to avoid race conditions on slow CI)
        for _ in range(20):
            catalog = await bus.request(GetToolCatalog())
            names = [t.name for t in catalog]
            if "mcp_tool_1" in names and "mcp_tool_2" in names:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("MCP tools not propagated to catalog within 200ms")

        catalog = await bus.request(GetToolCatalog())
        names = [t.name for t in catalog]
        assert "mcp_tool_1" in names
        assert "mcp_tool_2" in names

        mcp_tools = [t for t in catalog if t.provider == "mcp"]
        assert len(mcp_tools) == 2

        await bus.shutdown()


class TestTurnProcessorToolsViaBus:
    """TurnProcessor's bus-aware tool methods."""

    @pytest.mark.asyncio
    async def test_get_tool_catalog_via_bus(self):
        from alex.agent.turn_processor import TurnProcessor

        bus = AsyncEventBus()
        await bus.start()

        tools_mod = ToolsModule()
        await tools_mod.start(bus)

        from alex.agent.chat_service import _BusTurnServices
        tp = TurnProcessor(
            llm=None,
            push_notification=lambda e: None,
            services=_BusTurnServices(bus),
            get_system_prompt=lambda _: "",
            max_iterations=1,
        )

        catalog = await tp._get_tool_catalog()
        assert isinstance(catalog, list)

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_execute_tool_via_bus(self):
        from alex.agent.turn_processor import TurnProcessor

        bus = AsyncEventBus()
        await bus.start()

        tools_mod = ToolsModule()

        async def echo(msg: str = "") -> str:
            return f"echo: {msg}"

        tool = AlexTool.from_function(name="echo", description="Echo", coroutine=echo)
        tools_mod.register_tool(tool)
        await tools_mod.start(bus)

        from alex.agent.chat_service import _BusTurnServices
        tp = TurnProcessor(
            llm=None,
            push_notification=lambda e: None,
            services=_BusTurnServices(bus),
            get_system_prompt=lambda _: "",
            max_iterations=1,
        )

        import types
        ctx = types.SimpleNamespace(session_id="test", turn_id="t1", source="user")
        result = await tp._execute_tool(ctx, "echo", {"msg": "hello bus"})
        assert result.ok is True
        assert "echo: hello bus" in result.output

        await bus.shutdown()
