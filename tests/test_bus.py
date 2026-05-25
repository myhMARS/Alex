"""Tests for the async event bus — subscribe, publish, serialisation, isolation."""

import asyncio
from dataclasses import dataclass

import pytest

from alex.bus import AsyncEventBus
from alex.bus.events import (
    Event,
    TokenEmitted,
    TurnCompleted,
)


@dataclass
class _TestEvent(Event):
    value: str = ""


@pytest.mark.asyncio
async def test_start_and_shutdown():
    bus = AsyncEventBus()
    await bus.start()
    assert bus._running is True
    await bus.shutdown()
    assert bus._running is False


@pytest.mark.asyncio
async def test_publish_and_subscribe():
    bus = AsyncEventBus()
    await bus.start()

    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    await bus.subscribe(Event, handler)
    evt = _TestEvent(session_id="s1", value="hello")
    bus.publish(evt)
    # Give dispatch a moment
    await asyncio.sleep(0.1)
    await bus.shutdown()

    assert len(received) == 1
    assert received[0].session_id == "s1"


@pytest.mark.asyncio
async def test_subclass_matching():
    """Subscribers for Event should also receive TurnCompleted (subclass of DomainEvent → Event)."""
    bus = AsyncEventBus()
    await bus.start()

    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    await bus.subscribe(Event, handler)
    evt = TurnCompleted(session_id="s1", content="done")
    bus.publish(evt)
    await asyncio.sleep(0.1)
    await bus.shutdown()

    assert len(received) == 1
    assert isinstance(received[0], TurnCompleted)


@pytest.mark.asyncio
async def test_type_specific_subscription():
    """A handler for TokenEmitted should NOT receive TurnCompleted."""
    bus = AsyncEventBus()
    await bus.start()

    tokens: list[TokenEmitted] = []

    async def token_handler(event: TokenEmitted) -> None:
        tokens.append(event)

    await bus.subscribe(TokenEmitted, token_handler)
    bus.publish(TurnCompleted(session_id="s1"))
    bus.publish(TokenEmitted(session_id="s1", delta="hi"))
    await asyncio.sleep(0.1)
    await bus.shutdown()

    assert len(tokens) == 1
    assert isinstance(tokens[0], TokenEmitted)


@pytest.mark.asyncio
async def test_same_session_serial():
    """Events for the same session must be dispatched in order (serial)."""
    bus = AsyncEventBus()
    await bus.start()

    order: list[str] = []

    async def handler(event: Event) -> None:
        order.append(event.event_id)
        # Simulate work to expose any parallelism
        await asyncio.sleep(0.01)

    await bus.subscribe(Event, handler)

    e1 = _TestEvent(session_id="A", value="1")
    e2 = _TestEvent(session_id="A", value="2")
    e3 = _TestEvent(session_id="A", value="3")

    bus.publish(e1)
    bus.publish(e2)
    bus.publish(e3)

    await asyncio.sleep(0.2)
    await bus.shutdown()

    assert order == [e1.event_id, e2.event_id, e3.event_id]


@pytest.mark.asyncio
async def test_different_session_independent():
    """Events for different sessions are dispatched correctly and independently."""
    bus = AsyncEventBus()
    await bus.start()

    received: dict[str, list[str]] = {"a": [], "b": []}

    async def handler(event: _TestEvent) -> None:
        if isinstance(event, _TestEvent):
            received[event.session_id].append(event.value)

    await bus.subscribe(_TestEvent, handler)

    bus.publish(_TestEvent(session_id="a", value="a1"))
    bus.publish(_TestEvent(session_id="b", value="b1"))
    bus.publish(_TestEvent(session_id="a", value="a2"))

    await asyncio.sleep(0.2)
    await bus.shutdown()

    assert received["a"] == ["a1", "a2"]
    assert received["b"] == ["b1"]


@pytest.mark.asyncio
async def test_handler_exception_isolation():
    """One handler's exception must not affect other handlers."""
    bus = AsyncEventBus()
    await bus.start()

    healthy_received: list[Event] = []

    async def bad_handler(event: Event) -> None:
        raise RuntimeError("boom")

    async def good_handler(event: Event) -> None:
        healthy_received.append(event)

    await bus.subscribe(Event, bad_handler)
    await bus.subscribe(Event, good_handler)

    bus.publish(_TestEvent(session_id="s1"))
    await asyncio.sleep(0.1)
    await bus.shutdown()

    assert len(healthy_received) == 1


@pytest.mark.asyncio
async def test_unsubscribe():
    bus = AsyncEventBus()
    await bus.start()

    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    await bus.subscribe(Event, handler)
    bus.publish(_TestEvent(session_id="s1"))
    await asyncio.sleep(0.05)

    await bus.unsubscribe(Event, handler)
    bus.publish(_TestEvent(session_id="s2"))
    await asyncio.sleep(0.05)

    await bus.shutdown()
    assert len(received) == 1


@pytest.mark.asyncio
async def test_buffers_events_before_start():
    """Events published before start() are buffered and delivered after start."""
    bus = AsyncEventBus()

    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    await bus.subscribe(Event, handler)
    # Publish before start — should be buffered, not dropped
    bus.publish(_TestEvent(session_id="s1", value="before-start"))
    assert len(bus._pending) == 1

    await bus.start()
    await asyncio.sleep(0.1)

    assert len(received) == 1
    assert received[0].session_id == "s1"
    assert len(bus._pending) == 0

    await bus.shutdown()


@pytest.mark.asyncio
async def test_multiple_subscribers_same_event():
    bus = AsyncEventBus()
    await bus.start()

    h1: list[Event] = []
    h2: list[Event] = []

    async def handler1(event: Event) -> None:
        h1.append(event)

    async def handler2(event: Event) -> None:
        h2.append(event)

    await bus.subscribe(Event, handler1)
    await bus.subscribe(Event, handler2)
    bus.publish(_TestEvent(session_id="s1"))
    await asyncio.sleep(0.1)
    await bus.shutdown()

    assert len(h1) == 1
    assert len(h2) == 1
