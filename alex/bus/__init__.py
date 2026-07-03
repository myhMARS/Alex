"""Event bus — unified publish/subscribe + request/reply for all module coordination."""

from alex.bus.events import (
    Event,
    Command,
    DomainEvent,
    UIEvent,
    UserTurnRequested,
    CronTurnRequested,
    FeedbackSubmitted,
    TurnStarted,
    TurnCompleted,
    TurnFailed,
    ThinkingUpdated,
    TokenEmitted,
    ToolStarted,
    ToolFinished,
    ToolsProvided,
    SkillLoaded,
    SkillReflectEvent,
    SkillReflectErrorEvent,
    CronJobEvent,
    CronDebugEvent,
)
from alex.bus.in_memory import AsyncEventBus

# Re-export kernel bus primitives for convenience
from alex.kernel.bus import (
    Request,
    MessageBus,
    correlation_id,
)
from alex.kernel.errors import (
    CapabilityTimeout,
    CapabilityUnavailable,
    HandlerError,
)

__all__ = [
    # Event types
    "Event",
    "Command",
    "DomainEvent",
    "UIEvent",
    "UserTurnRequested",
    "CronTurnRequested",
    "FeedbackSubmitted",
    "TurnStarted",
    "TurnCompleted",
    "TurnFailed",
    "ThinkingUpdated",
    "TokenEmitted",
    "ToolStarted",
    "ToolFinished",
    "ToolsProvided",
    "SkillLoaded",
    "SkillReflectEvent",
    "SkillReflectErrorEvent",
    "CronJobEvent",
    "CronDebugEvent",
    # Bus implementation
    "AsyncEventBus",
    # Kernel bus primitives (request/reply)
    "Request",
    "MessageBus",
    "correlation_id",
    # Kernel errors
    "CapabilityTimeout",
    "CapabilityUnavailable",
    "HandlerError",
]
