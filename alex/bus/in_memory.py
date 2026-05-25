"""In-process async event bus with per-session serialisation.

Design:
  - Internal asyncio.Queue for pending events
  - Subscribers register by event type (isinstance check)
  - Events are dispatched serially by a single consumer loop
  - Events for the same session_id are further serialised via per-session locks
  - A handler's exception never crashes other handlers or the bus
  - cross-thread publish is safe via loop.call_soon_threadsafe
  - Events published before start() are buffered and drained on start
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from alex.bus.events import Event

logger = logging.getLogger(__name__)


class AsyncEventBus:
    """In-process publish/subscribe event bus."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._pending: list[Event] = []  # buffered before start()
        self._subscribers: dict[type, list[callable]] = defaultdict(list)
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()  # protects _subscribers mutations

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

    # ── subscribe / unsubscribe ───────────────────────────────────────────

    async def subscribe(self, event_type: type, handler: callable) -> None:
        """Register an async handler for events of *event_type* (isinstance match)."""
        async with self._lock:
            self._subscribers[event_type].append(handler)

    async def unsubscribe(self, event_type: type, handler: callable) -> None:
        """Remove a previously registered handler."""
        async with self._lock:
            handlers = self._subscribers.get(event_type, [])
            try:
                handlers.remove(handler)
            except ValueError:
                pass

    # ── publish ───────────────────────────────────────────────────────────

    def publish(self, event: Event) -> None:
        """Enqueue an event for dispatch.  Safe to call from any thread.

        Events published before start() are buffered and drained into the
        queue once the dispatch loop begins.
        """
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
            handlers: list[callable] = []
            for reg_type, reg_handlers in self._subscribers.items():
                if issubclass(event_type, reg_type):
                    handlers.extend(reg_handlers)

        # Dispatch to each handler independently
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
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
