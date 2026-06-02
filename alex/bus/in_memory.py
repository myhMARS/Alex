"""In-process async message bus with pub/sub and request/reply.

Design:
  - **Event plane**: asyncio.Queue → dispatch to all matching subscribers
    (existing behaviour, unchanged).  Per-session serialisation via locks.
  - **Request plane**: point-to-point with correlation-id + asyncio.Future.
    Registered via ``provide()``, called via ``request()``.
    The request plane does NOT use session locks (avoids deadlocks — see §7-A).

  - Internal ``asyncio.Queue`` for pending events
  - Subscribers register by event type (isinstance check)
  - Events are dispatched serially by a single consumer loop
  - Events for the same session_id are further serialised via per-session locks
  - A handler's exception never crashes other handlers or the bus
  - cross-thread publish is safe via loop.call_soon_threadsafe
  - Events published before start() are buffered and drained on start
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from alex.bus.events import Event
from alex.kernel.bus import Request as KernelRequest
from alex.kernel.errors import CapabilityTimeout, CapabilityUnavailable, HandlerError

logger = logging.getLogger(__name__)


class AsyncEventBus:
    """In-process publish/subscribe event bus with request/reply support."""

    def __init__(self) -> None:
        # ── event plane ──────────────────────────────────────────────────
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._pending: list[Event] = []  # buffered before start()
        self._subscribers: dict[type[Any], list[Callable[..., Any]]] = defaultdict(list)
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()  # protects _subscribers / _providers mutations

        # ── request plane ────────────────────────────────────────────────
        self._providers: dict[type[Any], Callable[..., Any]] = {}

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the dispatch loop.  Must be called once before publish().

        Any events published before start() are drained into the dispatch
        queue and will be delivered to subscribers.
        """
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        # Drain pre-start buffered events
        for event in self._pending:
            self._queue.put_nowait(event)
        self._pending.clear()

    async def shutdown(self) -> None:
        """Cancel the dispatch loop and drain outstanding events."""
        self._running = False
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
            self._dispatch_task = None
        # Drain queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # Clear providers
        self._providers.clear()

    # ── event plane: subscribe / unsubscribe ───────────────────────────────

    async def subscribe(self, event_type: type[Any], handler: Callable[..., Any]) -> None:
        """Register an async handler for events of *event_type* (isinstance match)."""
        async with self._lock:
            self._subscribers[event_type].append(handler)

    async def unsubscribe(self, event_type: type[Any], handler: Callable[..., Any]) -> None:
        """Remove a previously registered handler."""
        async with self._lock:
            handlers = self._subscribers.get(event_type, [])
            try:
                handlers.remove(handler)
            except ValueError:
                pass

    # ── event plane: publish ───────────────────────────────────────────────

    def publish(self, event: Event) -> None:
        """Enqueue an event for dispatch.  Safe to call from any thread.

        Events published before start() are buffered and drained into the
        queue once the dispatch loop begins.
        """
        logger.debug("bus.publish type=%s sid=%s", type(event).__name__, getattr(event, "session_id", ""))
        loop = self._loop
        if loop is None:
            self._pending.append(event)
            return
        try:
            cur = asyncio.get_running_loop()
        except RuntimeError:
            cur = None
        if cur is loop:
            self._queue.put_nowait(event)
        else:
            loop.call_soon_threadsafe(self._queue.put_nowait, event)

    # ── internal dispatch ─────────────────────────────────────────────────

    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                event = await self._queue.get()
            except asyncio.CancelledError:
                break
            except RuntimeError:
                break

            session_id = getattr(event, "session_id", "") or ""
            if session_id:
                lock = await self._get_session_lock(session_id)
                async with lock:
                    await self._dispatch_one(event)
            else:
                await self._dispatch_one(event)

    async def _dispatch_one(self, event: Event) -> None:
        event_type = type(event)
        async with self._lock:
            # Collect matching handlers — isinstance so subclasses match
            handlers: list[Callable[..., Any]] = []
            for reg_type, reg_handlers in self._subscribers.items():
                if issubclass(event_type, reg_type):
                    handlers.extend(reg_handlers)

        # Dispatch to each handler independently
        for handler in handlers:
            try:
                if inspect.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception:
                logger.warning(
                    "EventBus handler %s failed for %s",
                    getattr(handler, "__name__", handler),
                    event_type.__name__,
                    exc_info=True,
                )

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    # ── request plane: provide ─────────────────────────────────────────────

    def provide(self, request_type: type[Any], handler: Callable[..., Any]) -> None:
        """Register a handler for *request_type*.  Only ONE handler per type.

        The handler receives the request instance and returns the reply value.
        If a handler is already registered for *request_type*, it is replaced
        (last-write-wins — modules should not race on registration).
        """
        if not issubclass(request_type, KernelRequest):
            raise TypeError(
                f"request_type must be a subclass of Request, got {request_type.__name__}"
            )
        self._providers[request_type] = handler
        logger.debug("Registered provider for %s", request_type.__name__)

    # ── request plane: request ─────────────────────────────────────────────

    async def request(self, req: KernelRequest, *, timeout: float = 30.0) -> Any:
        """Send a request and await the reply from the single registered handler.

        The request plane does NOT use session locks — correlation-id pairing
        is sufficient for correctness, and avoiding the lock prevents deadlocks
        when a turn handler (which holds the session lock) calls request().

        Args:
            req: The request instance (must be a ``Request`` subclass).
            timeout: Maximum seconds to wait for a reply.

        Returns:
            The value returned by the handler.

        Raises:
            CapabilityUnavailable: No handler registered for this request type.
            CapabilityTimeout: No reply received within *timeout* seconds.
            HandlerError: The handler raised an exception.
        """
        req_type = type(req)

        # Look up the handler
        handler = self._providers.get(req_type)
        if handler is None:
            raise CapabilityUnavailable(req_type.__name__)

        # Stamp a correlation-id for tracing
        from alex.kernel.bus import correlation_id
        cid = correlation_id()
        req._correlation_id = cid
        logger.debug("bus.request type=%s cid=%s", req_type.__name__, cid)

        try:
            if inspect.iscoroutinefunction(handler):
                result = await asyncio.wait_for(handler(req), timeout=timeout)
            else:
                result = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, handler, req),
                    timeout=timeout,
                )
            logger.debug("bus.request done type=%s cid=%s", req_type.__name__, cid)
            return result
        except asyncio.TimeoutError:
            logger.warning("bus.request timeout type=%s cid=%s timeout=%s", req_type.__name__, cid, timeout)
            raise CapabilityTimeout(req_type.__name__, timeout, cid)
        except (CapabilityUnavailable, CapabilityTimeout, HandlerError):
            raise
        except Exception as exc:
            raise HandlerError(req_type.__name__, exc) from exc
