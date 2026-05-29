"""Markdown rendering helpers for the TUI.

Rich's :class:`rich.markdown.Markdown` is a ``RenderableType`` that can
be passed straight to a Textual :class:`textual.widgets.Static`, so the
bubble layer can keep its existing structure and only swap out the
*content* of the response widget.

Why we render at finalize time, not while streaming
---------------------------------------------------
Re-parsing Markdown on every token would be expensive and visually
noisy: lists/code-blocks reflow as the LLM writes them, and the user
would see the layout jump on each tick.  Instead, the streaming path
keeps using plain text via ``Static.update(text)``, and we only swap
the renderable at finalize/insert-tool time when the surrounding text
is committed and won't change again.

The user can opt out via ``ALEX_TUI_MARKDOWN=0`` (or programmatically
via :func:`set_markdown_enabled`); the renderer then returns the raw
string and Textual displays it verbatim.
"""

from __future__ import annotations

from typing import Any

from rich.markdown import Markdown

from alex.config import is_tui_markdown_enabled_by_default


def _initial_state() -> bool:
    return is_tui_markdown_enabled_by_default()


_markdown_enabled: bool = _initial_state()


def is_markdown_enabled() -> bool:
    return _markdown_enabled


def set_markdown_enabled(enabled: bool) -> None:
    """Toggle Markdown rendering at runtime (used by tests and the host)."""
    global _markdown_enabled
    _markdown_enabled = bool(enabled)


def render_response(text: str) -> Any:
    """Render finalized assistant text as Markdown — or fall back to plain text.

    Returns a Rich ``Markdown`` renderable when the feature is enabled
    and the input is non-empty.  Otherwise returns the raw string so the
    widget displays it verbatim (no surprise side-effects when the
    feature is off, or for blank prefixes).
    """
    payload = text or ""
    if not payload.strip():
        return payload
    if not _markdown_enabled:
        return payload
    return Markdown(
        payload,
        # ANSI-friendly themes integrate cleanly with Textual's terminal palette.
        code_theme="ansi_dark",
        inline_code_theme="ansi_dark",
    )
