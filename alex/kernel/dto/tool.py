"""ToolSpec / ToolResult / ToolExecutionContext — neutral tool DTOs for cross-module transfer.

``ToolSpec`` is the serialisable description of a tool (name, description,
JSON Schema).  The real ``AlexTool`` (with its callable coroutine) stays
inside the ``tools`` module and never crosses the bus.

``ToolResult`` carries the outcome of a tool execution back to the caller.

``ToolExecutionContext`` provides runtime context for every tool execution
(session_id, turn_id, source) without coupling to the agent host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolExecutionContext:
    """Runtime context injected into every tool execution.

    Lives in the kernel so agent and tools can share it without
    either module importing the other.
    """

    session_id: str
    turn_id: str | None = None
    source: str = "user"  # "user" | "cron" | "system"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    """A tool's public description — everything the agent needs to call it.

    The ``provider`` field enables the tools gateway to route execution
    to the right backend (builtin / mcp / plugin).
    """

    name: str
    description: str
    json_schema: dict[str, Any] = field(default_factory=dict)
    provider: str = "builtin"  # "builtin" | "mcp" | "plugin"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_openai_schema(self) -> dict[str, Any]:
        """Return an OpenAI function-calling tool descriptor."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.json_schema,
            },
        }


@dataclass
class ToolResult:
    """The outcome of a tool execution — returned to the agent via request/reply."""

    name: str
    output: str = ""
    error: str = ""
    run_id: str = ""

    @property
    def is_error(self) -> bool:
        return bool(self.error)

    @property
    def ok(self) -> bool:
        return not self.error
