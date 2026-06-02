"""Event bus serial semantics tests — ordering, isolation, thread safety."""

from __future__ import annotations

import asyncio
import threading

import pytest

from alex.bus.events import Event, TurnStarted


# ── helpers ───────────────────────────────────────────────────────────────────

@pytest.fixture
async def bus():
    """Start and yield a bus, then shut it down."""
    from alex.bus import AsyncEventBus
    b = AsyncEventBus()
    await b.start()
    yield b
    await b.shutdown()


def _run_in_thread(coro_func, *args):
    """Run an async function in a thread and return the result."""
    result = []
    err = []

    def _runner():
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result.append(loop.run_until_complete(coro_func(*args)))
        except Exception as e:
            err.append(e)
        finally:
            if loop is not None:
                loop.close()

    t = threading.Thread(target=_runner)
    t.start()
    t.join(timeout=5)
    if err:
        raise err[0]
    return result[0] if result else None


# ── serial dispatch ───────────────────────────────────────────────────────────

class TestSerialDispatch:
    """Events for the same session must be dispatched in order and never
    interleaved (per-session lock)."""

    @pytest.mark.asyncio
    async def test_same_session_events_are_serial(self, bus):
        received: list[str] = []

        async def _handler(event):
            received.append(f"{event.session_id}:{event.turn_id}")
            await asyncio.sleep(0.01)  # simulate work

        await bus.subscribe(TurnStarted, _handler)

        e1 = TurnStarted(session_id="s1", turn_id="t1", source="agent", kind="user")
        e2 = TurnStarted(session_id="s1", turn_id="t2", source="agent", kind="user")
        e3 = TurnStarted(session_id="s1", turn_id="t3", source="agent", kind="user")

        bus.publish(e1)
        bus.publish(e2)
        bus.publish(e3)

        await asyncio.sleep(0.15)

        assert received == ["s1:t1", "s1:t2", "s1:t3"]

    @pytest.mark.asyncio
    async def test_different_sessions_are_dispatched_sequentially(self, bus):
        """Events are dequeued sequentially regardless of session.
        Per-session locks prevent interleaving of same-session events,
        but the dispatch loop itself is single-threaded."""
        received: list[str] = []

        async def _handler(event):
            received.append(event.session_id)

        await bus.subscribe(TurnStarted, _handler)

        bus.publish(TurnStarted(session_id="sa", turn_id="ta", source="agent", kind="user"))
        bus.publish(TurnStarted(session_id="sb", turn_id="tb", source="agent", kind="user"))
        bus.publish(TurnStarted(session_id="sa", turn_id="tc", source="agent", kind="user"))

        await asyncio.sleep(0.1)

        assert len(received) == 3
        assert "sa" in received
        assert "sb" in received


# ── handler exception isolation ───────────────────────────────────────────────

class TestHandlerIsolation:
    """A failing handler must never crash the bus or prevent other
    handlers from receiving events."""

    @pytest.mark.asyncio
    async def test_failing_handler_does_not_block_others(self, bus):
        good_calls: list[str] = []

        async def _bad_handler(event):
            raise RuntimeError("simulated handler crash")

        async def _good_handler(event):
            good_calls.append(event.turn_id)

        await bus.subscribe(TurnStarted, _bad_handler)
        await bus.subscribe(TurnStarted, _good_handler)

        bus.publish(TurnStarted(session_id="s", turn_id="t42", source="agent", kind="user"))
        await asyncio.sleep(0.1)

        assert "t42" in good_calls


# ── buffered publish (pre-start) ──────────────────────────────────────────────

class TestBufferedPublish:
    """Events published before start() must be drained and delivered after
    the dispatch loop begins."""

    @pytest.mark.asyncio
    async def test_pre_start_events_are_drained(self):
        from alex.bus import AsyncEventBus

        b = AsyncEventBus()
        received: list[str] = []

        async def _handler(event):
            received.append(event.turn_id)

        await b.subscribe(TurnStarted, _handler)

        b.publish(TurnStarted(session_id="x", turn_id="pre1", source="agent", kind="user"))
        b.publish(TurnStarted(session_id="x", turn_id="pre2", source="agent", kind="user"))

        await b.start()
        await asyncio.sleep(0.1)
        await b.shutdown()

        assert "pre1" in received
        assert "pre2" in received


# ── subscribe / unsubscribe ──────────────────────────────────────────────────

class TestSubscribeUnsubscribe:
    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self, bus):
        received: list[str] = []

        async def _handler(event):
            received.append(event.turn_id)

        await bus.subscribe(TurnStarted, _handler)
        bus.publish(TurnStarted(session_id="s", turn_id="t1", source="agent", kind="user"))
        await asyncio.sleep(0.05)
        assert "t1" in received

        await bus.unsubscribe(TurnStarted, _handler)
        bus.publish(TurnStarted(session_id="s", turn_id="t2", source="agent", kind="user"))
        await asyncio.sleep(0.05)
        assert "t2" not in received

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_is_noop(self, bus):
        async def _handler(event):
            pass
        # Should not raise
        await bus.unsubscribe(TurnStarted, _handler)


# ── cross-thread publish ──────────────────────────────────────────────────────

class TestCrossThreadPublish:
    @pytest.mark.asyncio
    async def test_cross_thread_publish_is_safe(self, bus):
        received: list[str] = []

        async def _handler(event):
            received.append(event.turn_id)

        await bus.subscribe(TurnStarted, _handler)

        def _publish_from_thread():
            bus.publish(TurnStarted(session_id="t", turn_id="thread-t", source="agent", kind="user"))

        t = threading.Thread(target=_publish_from_thread)
        t.start()
        t.join(timeout=5)

        await asyncio.sleep(0.1)
        assert "thread-t" in received


# ── isinstance matching ──────────────────────────────────────────────────────

class TestIsinstanceMatching:
    """Subscribers registered for a base type must receive subclass events."""

    @pytest.mark.asyncio
    async def test_base_type_subscriber_receives_subclass(self, bus):
        received: list[str] = []

        async def _handler(event):
            received.append(type(event).__name__)

        await bus.subscribe(Event, _handler)
        bus.publish(TurnStarted(session_id="s", turn_id="t", source="agent", kind="user"))
        await asyncio.sleep(0.05)

        assert "TurnStarted" in received
