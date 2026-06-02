"""Message bus protocol — the only communication channel between modules.

Defines three message semantics:

* **Event** — broadcast pub/sub, fire-and-forget (e.g. TokenEmitted, TurnCompleted)
* **Command** — point-to-point, single handler, optional ack (e.g. ScheduleCron)
* **Request/Reply** — point-to-point, returns a value via correlation-id + future
  (e.g. ExecuteTool, GetContext)

All messages carry an optional ``trace_id`` for end-to-end debugging across
module boundaries.
"""

from __future__ import annotations

import uuid as _uuid
import time as _time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

# Result type variable for generic Request
T = TypeVar("T")


# ── correlation-id helper ────────────────────────────────────────────────────

def correlation_id() -> str:
    """Return a short unique id for request/reply pairing."""
    return _uuid.uuid4().hex[:12]


# ── base message types (in the kernel, not in bus/events.py) ──────────────────

@dataclass
class Event:
    """Base for every message on the bus — broadcast, fire-and-forget."""
    event_id: str = field(default_factory=lambda: _uuid.uuid4().hex[:12])
    session_id: str = ""
    turn_id: str = ""
    source: str = ""
    ts: float = field(default_factory=_time.time)
    trace_id: str = ""


@dataclass
class Command(Event):
    """Marker — a request to perform an action (point-to-point, optional ack)."""


@dataclass
class Request(Generic[T]):
    """Base for request/reply messages — point-to-point, returns a value.

    Subclasses declare their return type via the generic parameter::

        @dataclass
        class GetContext(Request[list[dict[str, Any]]]):
            ...

    This enables ``bus.request(GetContext(...))`` to return
    ``list[dict[str, Any]]`` at the type-checker level.
    """
    session_id: str = ""
    turn_id: str = ""
    trace_id: str = ""
    _correlation_id: str = field(default="", repr=False)


# ── handler type aliases ─────────────────────────────────────────────────────

EventHandler = Callable[[Event], Awaitable[None]]
ReqHandler = Callable[[Request], Awaitable[Any]]

T_Req = TypeVar("T_Req", bound=Request)


# ── MessageBus protocol ──────────────────────────────────────────────────────

class MessageBus(Protocol):
    """The one protocol every module depends on.

    **Event plane** (broadcast):
        publish() → all subscribers of that event type
        subscribe() → register an async handler

    **Request plane** (point-to-point with return value):
        request() → dispatch to the single registered handler, await reply
        provide()  → register a handler for a request type
    """

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None: ...
    async def shutdown(self) -> None: ...

    # ── event plane (broadcast) ────────────────────────────────────────────

    def publish(self, event: Event) -> None: ...

    async def subscribe(self, event_type: type, handler: EventHandler) -> None: ...

    async def unsubscribe(self, event_type: type, handler: EventHandler) -> None: ...

    # ── request plane (point-to-point, returns value) ──────────────────────

    async def request(self, req: Request[T], *, timeout: float = 30.0) -> T:
        """Send a request and await the reply from the single registered handler.

        The return type is inferred from the Request's generic parameter.

        Raises:
            CapabilityUnavailable: No handler registered for this request type.
            CapabilityTimeout: No reply received within *timeout* seconds.
            HandlerError: The handler raised an exception.
        """
        ...

    def provide(self, request_type: type, handler: ReqHandler) -> None:
        """Register a handler for *request_type*.  Only ONE handler per type."""
        ...
