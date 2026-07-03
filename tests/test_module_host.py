"""Integration tests for Module/ModuleHost pattern (Phase 2).

Each test verifies that a module can be registered with the host,
started, and that it responds to bus requests/events correctly.
"""

from dataclasses import dataclass
from typing import Any

import pytest

from alex.bus.in_memory import AsyncEventBus
from alex.kernel.bus import Request
from alex.kernel.host import ModuleHost


# ── Minimal test module ──────────────────────────────────────────────────────

@dataclass
class EchoRequest(Request):
    text: str = ""


class EchoModule:
    """A minimal module that provides an echo handler."""
    name = "echo"

    def __init__(self):
        self._bus = None
        self._started = False
        self._stopped = False

    async def start(self, bus):
        self._bus = bus
        self._started = True
        bus.provide(EchoRequest, self._echo)

    async def stop(self):
        self._stopped = True
        self._bus = None

    async def _echo(self, req: EchoRequest) -> str:
        return f"echo: {req.text}"


class ListenerModule:
    """A module that listens for events."""
    name = "listener"

    def __init__(self):
        self.received: list[Any] = []

    async def start(self, bus):
        from alex.kernel.bus import Event
        @dataclass
        class _Ping(Event):
            pass
        self._Ping = _Ping

        async def handler(event):
            self.received.append(event)
        await bus.subscribe(_Ping, handler)

    async def stop(self):
        pass


# ── Tests ────────────────────────────────────────────────────────────────────


class TestModuleHostLifecycle:
    @pytest.mark.asyncio
    async def test_register_and_start_module(self):
        bus = AsyncEventBus()
        host = ModuleHost(bus)

        echo = EchoModule()
        host.register(echo)

        await host.start_all()
        assert echo._started is True

        # Module should respond to bus requests
        result = await bus.request(EchoRequest(text="hello"))
        assert result == "echo: hello"

        await host.stop_all()
        assert echo._stopped is True

    @pytest.mark.asyncio
    async def test_multiple_modules(self):
        bus = AsyncEventBus()
        host = ModuleHost(bus)

        echo1 = EchoModule()
        echo2 = EchoModule()
        host.register(echo1)
        host.register(echo2)

        await host.start_all()

        # Second registration replaces first handler (last-write-wins)
        result = await bus.request(EchoRequest(text="world"))
        assert result == "echo: world"

        await host.stop_all()

    @pytest.mark.asyncio
    async def test_modules_in_registration_order(self):
        """Modules are started in registration order."""
        bus = AsyncEventBus()
        host = ModuleHost(bus)

        order: list[str] = []

        class OrderedModule:
            name = ""
            def __init__(self, n):
                self.name = n
            async def start(self, bus):
                order.append(self.name)
            async def stop(self):
                pass

        host.register(OrderedModule("first"))
        host.register(OrderedModule("second"))
        host.register(OrderedModule("third"))

        await host.start_all()
        assert order == ["first", "second", "third"]
        await host.stop_all()


class TestMemoryModule:
    @pytest.mark.asyncio
    async def test_start_and_provides_handlers(self):
        from alex.kernel.contracts.memory import AppendMessages, GetContext
        from alex.memory.module import MemoryModule

        bus = AsyncEventBus()
        host = ModuleHost(bus)

        mem = MemoryModule()
        host.register(mem)

        await host.start_all()

        # Initially empty
        ctx = await bus.request(GetContext(session_id="test"))
        assert ctx == []

        # Append
        await bus.request(AppendMessages(
            session_id="test",
            messages=[{"role": "user", "content": "hello"}],
        ))

        # Read back
        ctx = await bus.request(GetContext(session_id="test"))
        assert len(ctx) == 1
        assert ctx[0]["role"] == "user"
        assert ctx[0]["content"] == "hello"

        await host.stop_all()

    @pytest.mark.asyncio
    async def test_clear_memory(self):
        from alex.kernel.contracts.memory import (
            AppendMessages,
            ClearMemory,
            GetContext,
            ReplaceMemory,
        )
        from alex.memory.module import MemoryModule

        bus = AsyncEventBus()
        host = ModuleHost(bus)

        mem = MemoryModule()
        host.register(mem)
        await host.start_all()

        # Add messages
        await bus.request(AppendMessages(
            session_id="s1",
            messages=[{"role": "user", "content": "msg1"}],
        ))
        assert len(await bus.request(GetContext(session_id="s1"))) == 1

        # Clear
        await bus.request(ClearMemory(session_id="s1"))
        assert len(await bus.request(GetContext(session_id="s1"))) == 0

        # Replace
        await bus.request(ReplaceMemory(
            session_id="s1",
            messages=[{"role": "user", "content": "replaced"}],
        ))
        ctx = await bus.request(GetContext(session_id="s1"))
        assert len(ctx) == 1
        assert ctx[0]["content"] == "replaced"

        await host.stop_all()

    @pytest.mark.asyncio
    async def test_read_after_write(self):
        """§7-B: Append must be awaited so immediate GetContext sees the write."""
        from alex.kernel.contracts.memory import AppendMessages, GetContext
        from alex.memory.module import MemoryModule

        bus = AsyncEventBus()
        host = ModuleHost(bus)

        mem = MemoryModule()
        host.register(mem)
        await host.start_all()

        # Simulate agent flow: append → read
        await bus.request(AppendMessages(
            session_id="flow",
            messages=[
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
            ],
        ))
        ctx = await bus.request(GetContext(session_id="flow"))
        assert len(ctx) == 2
        assert ctx[0]["content"] == "q1"
        assert ctx[1]["content"] == "a1"

        await host.stop_all()


class TestSkillModule:
    @pytest.mark.asyncio
    async def test_load_nonexistent_skill_raises(self):
        from alex.skill.module import SkillModule
        from alex.kernel.contracts.skills import LoadSkill
        from alex.kernel.errors import HandlerError

        bus = AsyncEventBus()
        host = ModuleHost(bus)

        skill_mod = SkillModule()
        host.register(skill_mod)
        await host.start_all()

        with pytest.raises(HandlerError, match="Skill not found"):
            await bus.request(LoadSkill(skill_name="nonexistent"))

        await host.stop_all()


class TestToolsModule:
    @pytest.mark.asyncio
    async def test_execute_unknown_tool_returns_error(self):
        from alex.tools.module import ToolsModule
        from alex.kernel.contracts.tools import ExecuteTool
        from alex.kernel.dto.tool import ToolExecutionContext

        bus = AsyncEventBus()
        host = ModuleHost(bus)

        tools_mod = ToolsModule()
        host.register(tools_mod)
        await host.start_all()

        ctx = ToolExecutionContext(session_id="test")
        result = await bus.request(ExecuteTool(name="nonexistent_tool", args={}, ctx=ctx))
        # ToolExecutor returns "Error: ..." in output for unknown tools
        assert "Error" in result.output or result.error != ""

        await host.stop_all()

    @pytest.mark.asyncio
    async def test_register_and_execute_tool(self):
        from alex.tools.module import ToolsModule
        from alex.kernel.contracts.tools import ExecuteTool, GetToolCatalog
        from alex.tools.models import AlexTool
        from alex.kernel.dto.tool import ToolExecutionContext

        bus = AsyncEventBus()
        host = ModuleHost(bus)

        tools_mod = ToolsModule()

        # Register a simple tool
        async def hello_tool(name: str = "World") -> str:
            return f"Hello, {name}!"

        tool = AlexTool.from_function(
            name="hello",
            description="Say hello",
            coroutine=hello_tool,
        )
        tools_mod.register_tool(tool)

        host.register(tools_mod)
        await host.start_all()

        # Catalog should include the tool
        catalog = await bus.request(GetToolCatalog())
        names = [t.name for t in catalog]
        assert "hello" in names

        # Execute it
        ctx = ToolExecutionContext(session_id="test")
        result = await bus.request(ExecuteTool(name="hello", args={"name": "Bus"}, ctx=ctx))
        assert result.ok is True
        assert "Hello, Bus!" in result.output

        await host.stop_all()


