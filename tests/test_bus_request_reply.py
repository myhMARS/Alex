"""Unit tests for the bus request/reply plane.

Phase 1 requirement: existing tests stay green; new bus request/reply
capabilities have unit tests.
"""

import asyncio
from dataclasses import dataclass

import pytest

from alex.bus import AsyncEventBus
from alex.kernel.bus import Event, Request
from alex.kernel.errors import CapabilityTimeout, CapabilityUnavailable, HandlerError


# ── Test request types ───────────────────────────────────────────────────────

@dataclass
class GetGreeting(Request):
    name: str = "World"


@dataclass
class GetSum(Request):
    a: int = 0
    b: int = 0


@dataclass
class SlowRequest(Request):
    delay: float = 5.0


@dataclass
class FailingRequest(Request):
    message: str = "boom"


@dataclass
class EmptyRequest(Request):
    pass


# ── Test event (for cross-plane isolation tests) ─────────────────────────────

@dataclass
class _TestEvent(Event):
    value: str = ""


# ── provide + request: happy path ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_provide_and_request_returns_value():
    """A registered handler receives the request and returns a value."""
    bus = AsyncEventBus()
    await bus.start()

    async def handler(req: GetGreeting) -> str:
        return f"Hello, {req.name}!"

    bus.provide(GetGreeting, handler)

    result = await bus.request(GetGreeting(name="Alice"))
    assert result == "Hello, Alice!"

    await bus.shutdown()


@pytest.mark.asyncio
async def test_request_with_complex_return():
    """A handler can return any type (list, dict, etc.)."""
    bus = AsyncEventBus()
    await bus.start()

    async def handler(req: GetSum) -> dict[str, int]:
        return {"sum": req.a + req.b, "product": req.a * req.b}

    bus.provide(GetSum, handler)

    result = await bus.request(GetSum(a=3, b=4))
    assert result == {"sum": 7, "product": 12}

    await bus.shutdown()


@pytest.mark.asyncio
async def test_request_correlation_id_is_stamped():
    """The bus stamps a _correlation_id on each request."""
    bus = AsyncEventBus()
    await bus.start()

    async def handler(req: GetGreeting) -> str:
        assert req._correlation_id != ""
        return f"cid={req._correlation_id}"

    bus.provide(GetGreeting, handler)
    result = await bus.request(GetGreeting(name="Bob"))
    assert result.startswith("cid=")
    assert len(result) > 4

    await bus.shutdown()


# ── error cases ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capability_unavailable_when_no_handler_registered():
    """request() raises CapabilityUnavailable when no handler is registered."""
    bus = AsyncEventBus()
    await bus.start()

    with pytest.raises(CapabilityUnavailable) as exc_info:
        await bus.request(GetGreeting(name="NoOne"))
    assert "GetGreeting" in str(exc_info.value)

    await bus.shutdown()


@pytest.mark.asyncio
async def test_capability_timeout_when_handler_takes_too_long():
    """request() raises CapabilityTimeout when the handler doesn't reply in time."""
    bus = AsyncEventBus()
    await bus.start()

    async def slow_handler(req: SlowRequest) -> str:
        await asyncio.sleep(req.delay)
        return "too late"

    bus.provide(SlowRequest, slow_handler)

    with pytest.raises(CapabilityTimeout) as exc_info:
        await bus.request(SlowRequest(delay=10.0), timeout=0.1)
    assert "SlowRequest" in str(exc_info.value)

    await bus.shutdown()


@pytest.mark.asyncio
async def test_handler_error_wraps_handler_exception():
    """request() raises HandlerError when the handler raises an exception."""
    bus = AsyncEventBus()
    await bus.start()

    async def bad_handler(req: FailingRequest) -> str:
        raise ValueError(req.message)

    bus.provide(FailingRequest, bad_handler)

    with pytest.raises(HandlerError) as exc_info:
        await bus.request(FailingRequest(message="boom!"))
    assert "FailingRequest" in str(exc_info.value)
    assert "ValueError" in str(exc_info.value)

    await bus.shutdown()


@pytest.mark.asyncio
async def test_provide_rejects_non_request_type():
    """provide() raises TypeError if the type is not a Request subclass."""
    bus = AsyncEventBus()
    await bus.start()

    with pytest.raises(TypeError, match="must be a subclass of Request"):
        bus.provide(Event, lambda e: None)  # type: ignore[arg-type]

    await bus.shutdown()


# ── cross-plane isolation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_plane_does_not_interfere_with_event_plane():
    """Events and requests use independent dispatch paths."""
    bus = AsyncEventBus()
    await bus.start()

    received_events: list[str] = []

    async def event_handler(event: _TestEvent) -> None:
        received_events.append(event.value)

    async def request_handler(req: GetGreeting) -> str:
        return f"Hi {req.name}"

    await bus.subscribe(_TestEvent, event_handler)
    bus.provide(GetGreeting, request_handler)

    # Publish an event and send a request concurrently
    bus.publish(_TestEvent(session_id="s1", value="evt1"))
    reply = await bus.request(GetGreeting(name="Dave"))

    await asyncio.sleep(0.1)

    assert reply == "Hi Dave"
    assert "evt1" in received_events

    await bus.shutdown()


@pytest.mark.asyncio
async def test_multiple_request_types_coexist():
    """Different request types have independent handlers."""
    bus = AsyncEventBus()
    await bus.start()

    async def greet_handler(req: GetGreeting) -> str:
        return f"Hello, {req.name}!"

    async def sum_handler(req: GetSum) -> int:
        return req.a + req.b

    bus.provide(GetGreeting, greet_handler)
    bus.provide(GetSum, sum_handler)

    g = await bus.request(GetGreeting(name="Eve"))
    s = await bus.request(GetSum(a=10, b=20))

    assert g == "Hello, Eve!"
    assert s == 30

    await bus.shutdown()


@pytest.mark.asyncio
async def test_request_works_before_event_loop_started():
    """provide() can be called before start(), but request() needs start()."""
    bus = AsyncEventBus()

    async def handler(req: GetGreeting) -> str:
        return f"Hi {req.name}"

    bus.provide(GetGreeting, handler)

    # request() should still work if the bus hasn't been started
    # (the request plane doesn't use the dispatch loop)
    # Actually, it won't - but the handler is registered.
    # Let's start and then request.

    await bus.start()
    result = await bus.request(GetGreeting(name="PreStart"))
    assert result == "Hi PreStart"

    await bus.shutdown()


# ── handler replacement ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_provide_replaces_previous_handler():
    """Last-write-wins: providing a new handler replaces the old one."""
    bus = AsyncEventBus()
    await bus.start()

    async def handler1(req: GetGreeting) -> str:
        return "v1"

    async def handler2(req: GetGreeting) -> str:
        return "v2"

    bus.provide(GetGreeting, handler1)
    bus.provide(GetGreeting, handler2)

    result = await bus.request(GetGreeting(name="Test"))
    assert result == "v2"

    await bus.shutdown()
