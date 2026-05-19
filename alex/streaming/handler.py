"""Streaming event handler for token-level output."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class StreamEvent:
    """Unified stream event emitted during agent execution."""

    type: str  # "token", "tool_start", "tool_end", "skill_load", "done", "error"
    data: Any = None
    metadata: dict = field(default_factory=dict)


Listener = Callable[[StreamEvent], None]


class StreamHandler:
    """Manages stream listeners, dispatches events.

    Usage:
        handler = StreamHandler()
        handler.add_listener(terminal_printer)

        async for event in handler.wrap(agent.chat_stream("hello")):
            ...
    """

    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        self._listeners.remove(listener)

    def dispatch(self, event: StreamEvent) -> None:
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                pass

    async def wrap(self, stream: AsyncIterator[StreamEvent]) -> AsyncIterator[StreamEvent]:
        """Wrap an event stream, dispatching each event to listeners."""
        async for event in stream:
            self.dispatch(event)
            yield event
        self.dispatch(StreamEvent(type="done"))
