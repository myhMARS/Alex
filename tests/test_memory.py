"""Tests for BufferMemory with session_id and MemoryService Protocol conformance."""

import pytest
from alex import messages as msg
from alex.memory.buffer import BufferMemory


class TestBufferMemory:
    @pytest.mark.asyncio
    async def test_add_message_with_session_id(self):
        mem = BufferMemory()
        m = msg.user_message("hello")
        await mem.add_message(m, session_id="s1")
        assert mem.size == 1

    @pytest.mark.asyncio
    async def test_add_messages_with_session_id(self):
        mem = BufferMemory()
        msgs = [msg.user_message("a"), msg.user_message("b")]
        await mem.add_messages(msgs, session_id="s1")
        assert mem.size == 2

    @pytest.mark.asyncio
    async def test_append_protocol_method(self):
        mem = BufferMemory()
        msgs = [msg.user_message("a"), msg.user_message("b")]
        await mem.append(session_id="s1", messages=msgs)
        assert mem.size == 2

    @pytest.mark.asyncio
    async def test_get_context_with_session_id(self):
        mem = BufferMemory()
        m = msg.user_message("hello")
        await mem.add_message(m, session_id="s1")
        ctx = await mem.get_context(session_id="s1")
        assert len(ctx) == 1
        assert ctx[0]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_clear_with_session_id(self):
        mem = BufferMemory()
        await mem.add_message(msg.user_message("hello"), session_id="s1")
        assert mem.size == 1
        await mem.clear(session_id="s1")
        assert mem.size == 0

    @pytest.mark.asyncio
    async def test_replace_protocol_method(self):
        mem = BufferMemory()
        await mem.add_message(msg.user_message("old"), session_id="s1")
        new_msgs = [msg.assistant_message("new")]
        await mem.replace(session_id="s1", messages=new_msgs)
        assert mem.size == 1
        ctx = await mem.get_context(session_id="s1")
        assert ctx[0]["content"] == "new"

    def test_get_context_sync_with_session_id(self):
        mem = BufferMemory()
        mem._messages["s1"] = [msg.user_message("sync")]
        ctx = mem.get_context_sync(session_id="s1")
        assert len(ctx) == 1
        assert ctx[0]["content"] == "sync"

    @pytest.mark.asyncio
    async def test_sliding_window(self):
        mem = BufferMemory(max_size=3)
        for i in range(5):
            await mem.add_message(msg.user_message(str(i)), session_id="s1")
        assert mem.size == 3
        ctx = await mem.get_context(session_id="s1")
        assert ctx[0]["content"] == "2"
        assert ctx[-1]["content"] == "4"

    @pytest.mark.asyncio
    async def test_serial_writes_via_lock(self):
        """Concurrent writes should not interleave."""
        mem = BufferMemory()
        import asyncio

        async def writer(start: int):
            for i in range(start, start + 10):
                await mem.add_message(msg.user_message(str(i)), session_id="s1")
                await asyncio.sleep(0)

        await asyncio.gather(writer(0), writer(10))
        assert mem.size == 20

    @pytest.mark.asyncio
    async def test_multi_session_isolation(self):
        """Different sessions have independent message buffers."""
        mem = BufferMemory()
        await mem.add_message(msg.user_message("a1"), session_id="A")
        await mem.add_message(msg.user_message("b1"), session_id="B")
        await mem.add_message(msg.user_message("a2"), session_id="A")

        ctx_a = await mem.get_context(session_id="A")
        ctx_b = await mem.get_context(session_id="B")
        assert [m["content"] for m in ctx_a] == ["a1", "a2"]
        assert [m["content"] for m in ctx_b] == ["b1"]
        assert mem.size == 3

    @pytest.mark.asyncio
    async def test_multi_session_independent_clear(self):
        """Clearing one session does not affect another."""
        mem = BufferMemory()
        await mem.add_message(msg.user_message("a"), session_id="A")
        await mem.add_message(msg.user_message("b"), session_id="B")
        await mem.clear(session_id="A")
        assert len(await mem.get_context(session_id="A")) == 0
        assert len(await mem.get_context(session_id="B")) == 1

    @pytest.mark.asyncio
    async def test_multi_session_sliding_window_per_session(self):
        """Sliding window is applied per session."""
        mem = BufferMemory(max_size=2)
        for i in range(4):
            await mem.add_message(msg.user_message(str(i)), session_id="A")
        await mem.add_message(msg.user_message("b"), session_id="B")
        ctx_a = await mem.get_context(session_id="A")
        ctx_b = await mem.get_context(session_id="B")
        assert len(ctx_a) == 2
        assert [m["content"] for m in ctx_a] == ["2", "3"]
        assert [m["content"] for m in ctx_b] == ["b"]
