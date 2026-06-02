"""Phase 3 tests: memory accessed through bus request/reply.

Validates:
- Agent turn processor can use bus for GetContext and AppendMessages
- Read-after-write consistency (Section 7-B)
- Fallback to direct memory when bus is unavailable
- reasoning_content preserved through bus round-trip
"""

import pytest

from alex.bus.in_memory import AsyncEventBus
from alex.kernel.contracts.memory import AppendMessages, GetContext
from alex.memory.module import MemoryModule


class TestMemoryBusIntegration:
    """Memory operations through the bus with MemoryModule."""

    @pytest.mark.asyncio
    async def test_read_after_write_through_bus(self):
        """Section 7-B: Append must be awaited so GetContext sees the write."""
        bus = AsyncEventBus()
        memory_mod = MemoryModule()
        await bus.start()
        await memory_mod.start(bus)

        sid = "test-session"

        # Write
        await bus.request(AppendMessages(
            session_id=sid,
            messages=[
                {"role": "user", "content": "question 1"},
                {"role": "assistant", "content": "answer 1", "reasoning_content": "deep thought"},
            ],
        ))

        # Read — must see the just-written messages
        ctx = await bus.request(GetContext(session_id=sid))
        assert len(ctx) == 2
        assert ctx[0]["role"] == "user"
        assert ctx[0]["content"] == "question 1"
        assert ctx[1]["role"] == "assistant"
        assert ctx[1]["content"] == "answer 1"

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_reasoning_content_preserved(self):
        """Constraint 1: reasoning_content must survive bus round-trip."""
        bus = AsyncEventBus()
        memory_mod = MemoryModule()
        await bus.start()
        await memory_mod.start(bus)

        sid = "reasoning-test"
        reasoning = "Step-by-step reasoning for this answer.\nMulti-line thinking."

        await bus.request(AppendMessages(
            session_id=sid,
            messages=[
                {"role": "user", "content": "complex question"},
                {"role": "assistant", "content": "answer", "reasoning_content": reasoning},
            ],
        ))

        ctx = await bus.request(GetContext(session_id=sid))
        assert len(ctx) == 2
        assert ctx[1].get("reasoning_content", "") == reasoning

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_multiple_appends_maintain_order(self):
        """Section 7 constraint: turn order must be consistent."""
        bus = AsyncEventBus()
        memory_mod = MemoryModule()
        await bus.start()
        await memory_mod.start(bus)

        sid = "order-test"

        # Append turn 1
        await bus.request(AppendMessages(session_id=sid, messages=[
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "reply1"},
        ]))

        # Append turn 2
        await bus.request(AppendMessages(session_id=sid, messages=[
            {"role": "user", "content": "msg2"},
            {"role": "assistant", "content": "reply2"},
        ]))

        ctx = await bus.request(GetContext(session_id=sid))
        contents = [m["content"] for m in ctx]
        assert contents == ["msg1", "reply1", "msg2", "reply2"]

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_session_isolation(self):
        """Different sessions should have independent memory."""
        bus = AsyncEventBus()
        memory_mod = MemoryModule()
        await bus.start()
        await memory_mod.start(bus)

        await bus.request(AppendMessages(session_id="s1", messages=[
            {"role": "user", "content": "s1-msg"},
        ]))
        await bus.request(AppendMessages(session_id="s2", messages=[
            {"role": "user", "content": "s2-msg"},
        ]))

        ctx1 = await bus.request(GetContext(session_id="s1"))
        ctx2 = await bus.request(GetContext(session_id="s2"))

        assert len(ctx1) == 1
        assert ctx1[0]["content"] == "s1-msg"
        assert len(ctx2) == 1
        assert ctx2[0]["content"] == "s2-msg"

        await bus.shutdown()


class TestTurnProcessorMemoryViaBus:
    """Verify that TurnProcessor can use bus-based memory operations."""

    @pytest.mark.asyncio
    async def test_turn_processor_bus_memory_requires_handler(self):
        """TurnProcessor raises when bus has no memory handler (no fallback)."""
        from alex.agent.turn_processor import TurnProcessor
        from alex.agent.chat_service import _BusTurnServices

        bus = AsyncEventBus()
        await bus.start()

        tp = TurnProcessor(
            llm=None,
            push_notification=lambda e: None,
            services=_BusTurnServices(bus),
            get_system_prompt=lambda: "You are helpful.",
            max_iterations=1,
            session_id="test-sid",
        )

        # Without a MemoryModule registered, bus request raises CapabilityUnavailable
        from alex.kernel.errors import CapabilityUnavailable
        with pytest.raises(CapabilityUnavailable):
            await tp._get_memory_context("test-sid")

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_turn_processor_uses_bus_when_handler_available(self):
        """When bus has a MemoryModule, TurnProcessor uses bus requests."""
        from alex.agent.turn_processor import TurnProcessor
        from alex.agent.chat_service import _BusTurnServices

        bus = AsyncEventBus()
        await bus.start()

        # Register MemoryModule on the bus
        memory_mod = MemoryModule()
        await memory_mod.start(bus)

        tp = TurnProcessor(
            llm=None,
            push_notification=lambda e: None,
            services=_BusTurnServices(bus),
            get_system_prompt=lambda: "You are helpful.",
            max_iterations=1,
            session_id="bus-test-sid",
        )

        # Write through bus
        await tp._append_memory("bus-test-sid", [
            {"role": "user", "content": "bus message"},
        ])

        # Read through bus
        ctx = await tp._get_memory_context("bus-test-sid")
        assert len(ctx) == 1
        assert ctx[0]["content"] == "bus message"

        await bus.shutdown()
