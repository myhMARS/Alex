"""Shared kernel — DTOs, protocols, and contracts with zero business logic.

All six business modules (tui / agent / tools / mcp / skill / memory) are
allowed to depend on ``alex.kernel``.  They must NOT import each other.

The kernel must not import heavyweight third-party packages (textual,
openai, apscheduler, etc.).
"""

from alex.kernel.bus import (
    Command,
    Event,
    MessageBus,
    Request,
    correlation_id,
)
from alex.kernel.errors import (
    CapabilityTimeout,
    CapabilityUnavailable,
    HandlerError,
)
from alex.kernel.runtime import (
    Module,
    ModuleHost,
)

__all__ = [
    # Bus protocol
    "MessageBus",
    "Event",
    "Command",
    "Request",
    "correlation_id",
    # Runtime protocols
    "Module",
    "ModuleHost",
    # Errors
    "CapabilityTimeout",
    "CapabilityUnavailable",
    "HandlerError",
]
