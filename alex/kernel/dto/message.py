"""MessageDTO — neutral message representation for cross-module transfer.

Replaces direct ``BaseMessage`` / ``dict`` passing across module boundaries.
The DTO MUST be able to carry ``reasoning_content`` losslessly (Constraint 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MessageDTO:
    """A single chat message in a neutral, serialisable form.

    Fields match the OpenAI Chat Completions message shape so that
    no conversion is needed when feeding the LLM.
    """

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    reasoning_content: str = ""  # DeepSeek thinking mode — must survive round-trip
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to an OpenAI-compatible message dict."""
        d: dict[str, Any] = {"role": self.role}
        if self.content:
            d["content"] = self.content
        else:
            d["content"] = ""
        if self.tool_calls is not None:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            d["name"] = self.name
        if self.reasoning_content:
            d["reasoning_content"] = self.reasoning_content
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MessageDTO":
        """Create a MessageDTO from an OpenAI-compatible message dict."""
        return cls(
            role=str(d.get("role", "")),
            content=str(d.get("content", "")),
            tool_calls=d.get("tool_calls"),
            tool_call_id=d.get("tool_call_id"),
            name=d.get("name"),
            reasoning_content=str(d.get("reasoning_content", "")),
        )
