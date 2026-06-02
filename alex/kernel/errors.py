"""Shared error types for the message bus.

These are the only errors that cross module boundaries via the bus.
Modules may wrap or subclass them but should not invent new cross-cutting
error hierarchies.
"""

from __future__ import annotations


class CapabilityUnavailable(RuntimeError):
    """Raised when a ``request()`` targets a capability with no registered handler.

    This is the signal that a module is absent — callers should either
    degrade gracefully (optional capabilities) or surface a clear error
    (core capabilities).
    """

    def __init__(self, request_type: str) -> None:
        super().__init__(f"No handler registered for {request_type}")
        self.request_type = request_type


class CapabilityTimeout(RuntimeError):
    """Raised when a ``request()`` does not receive a reply within the deadline."""

    def __init__(self, request_type: str, timeout: float, correlation_id: str = "") -> None:
        msg = f"Request {request_type} timed out after {timeout:.1f}s"
        if correlation_id:
            msg += f" (correlation_id={correlation_id})"
        super().__init__(msg)
        self.request_type = request_type
        self.timeout = timeout
        self.correlation_id = correlation_id


class HandlerError(RuntimeError):
    """Wraps an exception raised inside a registered request handler.

    The original exception is chained via ``__cause__`` so tracebacks
    are preserved for debugging.
    """

    def __init__(self, request_type: str, original: Exception) -> None:
        super().__init__(f"Handler for {request_type} raised {type(original).__name__}: {original}")
        self.request_type = request_type
        self.original = original
