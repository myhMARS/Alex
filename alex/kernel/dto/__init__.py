"""Neutral DTOs shared across module boundaries.

These replace direct cross-module imports of domain objects
(e.g. BaseMessage, AlexTool, Skill).  Every DTO is a plain
dataclass — no business logic, no callables, no heavy deps.
"""

from alex.kernel.dto.message import MessageDTO
from alex.kernel.dto.skill import SkillCard
from alex.kernel.dto.tool import ToolExecutionContext, ToolResult, ToolSpec

__all__ = [
    "MessageDTO",
    "SkillCard",
    "ToolExecutionContext",
    "ToolResult",
    "ToolSpec",
]
