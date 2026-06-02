"""Tool approval DTOs — live in the kernel so TUI can import them without
depending on the tools business module."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PreviewBlock:
    """A titled preview chunk shown in the permission confirm modal.

    ``kind`` controls how the body is rendered:

    - ``text`` — plain text (default)
    - ``diff`` — unified diff with syntax highlighting
    - ``code`` — fixed-width body, no syntax highlighting
    """

    title: str
    body: str
    kind: str = "text"


@dataclass
class ToolApprovalRequest:
    """Everything the confirm hook needs to render a meaningful prompt."""

    tool_name: str
    permission: str
    args: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    preview: list[PreviewBlock] = field(default_factory=list)
