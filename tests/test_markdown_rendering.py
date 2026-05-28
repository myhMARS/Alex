"""Tests for the TUI Markdown rendering layer.

These tests pin two contracts:

1. ``render_response`` returns a Rich ``Markdown`` renderable for
   non-blank input when the feature is on, and falls back to the raw
   string otherwise.  This is what lets the bubble layer swap content
   without touching its widget structure.

2. The presenter actually uses Markdown rendering on the *finalized*
   text — including the response-prefix that gets emitted when a tool
   call interrupts a streaming response — but **not** during streaming
   (that path keeps using plain text via ``Static.update``).
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from rich.markdown import Markdown
from textual.app import App, ComposeResult
from textual.widgets import Static

from alex.tui.markdown import (
    is_markdown_enabled,
    render_response,
    set_markdown_enabled,
)
from alex.tui.presenter import AlexBubble, ToolBubble
from alex.tui.view_models import ChatTurn


@pytest.fixture
def markdown_on():
    prev = is_markdown_enabled()
    set_markdown_enabled(True)
    yield
    set_markdown_enabled(prev)


@pytest.fixture
def markdown_off():
    prev = is_markdown_enabled()
    set_markdown_enabled(False)
    yield
    set_markdown_enabled(prev)


# ── render_response ────────────────────────────────────────────────────


class TestRenderResponse:
    def test_blank_input_passes_through(self, markdown_on):
        # A blank string never becomes a Markdown renderable — that
        # would render as an awkward empty block in the bubble.
        assert render_response("") == ""
        assert render_response("   ") == "   "

    def test_returns_markdown_when_enabled(self, markdown_on):
        result = render_response("# Heading\n\nbody")
        assert isinstance(result, Markdown)

    def test_returns_plain_when_disabled(self, markdown_off):
        result = render_response("# Heading\n\nbody")
        assert isinstance(result, str)
        assert result.startswith("# Heading")

    def test_set_enabled_is_reversible(self):
        prev = is_markdown_enabled()
        set_markdown_enabled(False)
        assert is_markdown_enabled() is False
        set_markdown_enabled(True)
        assert is_markdown_enabled() is True
        set_markdown_enabled(prev)

    def test_env_var_off_disables_at_import(self, monkeypatch):
        # Re-importing the module with the env var set drops the flag.
        monkeypatch.setenv("ALEX_TUI_MARKDOWN", "0")
        import importlib

        from alex.tui import markdown as md_module

        reloaded = importlib.reload(md_module)
        try:
            assert reloaded.is_markdown_enabled() is False
        finally:
            # Restore the global state so other tests aren't disturbed.
            monkeypatch.delenv("ALEX_TUI_MARKDOWN", raising=False)
            importlib.reload(md_module)


# ── presenter integration ─────────────────────────────────────────────


class _BubbleHarness(App[None]):
    def __init__(self, bubble: AlexBubble) -> None:
        super().__init__()
        self._bubble = bubble

    def compose(self) -> ComposeResult:
        yield self._bubble


def _response_static(bubble: AlexBubble) -> Static:
    children = list(bubble.children)
    response = next(
        c for c in children if "response-text" in getattr(c, "classes", set())
    )
    assert isinstance(response, Static)
    return response


def _stored_renderable(static: Static):
    """Return the renderable Static was constructed/updated with.

    Textual stores it on the private ``_Static__content`` attribute
    (name-mangled because the class attribute is ``__content``).  We
    read it directly so the test asserts on what the bubble actually
    holds rather than what gets returned by ``render()`` after Textual
    has wrapped it for the rendering pipeline.
    """
    return getattr(static, "_Static__content")


@pytest.mark.asyncio
async def test_finalize_renders_response_as_markdown(markdown_on):
    bubble = AlexBubble()
    turn = ChatTurn(
        user_input="ping",
        response="# Heading\n\n- bullet\n- another",
    )
    async with _BubbleHarness(bubble).run_test() as pilot:
        bubble.finalize(turn)
        await pilot.pause()
        renderable = _stored_renderable(_response_static(bubble))
        assert isinstance(renderable, Markdown)


@pytest.mark.asyncio
async def test_finalize_uses_plain_string_when_disabled(markdown_off):
    bubble = AlexBubble()
    turn = ChatTurn(
        user_input="ping",
        response="# Heading\n\n- bullet",
    )
    async with _BubbleHarness(bubble).run_test() as pilot:
        bubble.finalize(turn)
        await pilot.pause()
        renderable = _stored_renderable(_response_static(bubble))
        # When markdown is off, the raw string flows straight through.
        assert isinstance(renderable, str)
        assert renderable.startswith("# Heading")


@pytest.mark.asyncio
async def test_streaming_set_response_stays_plain(markdown_on):
    """Per-token updates must not pay the Markdown re-parse cost.

    The user-turn streaming path calls ``bubble.set_response(text)`` on
    every UI tick.  Re-rendering Markdown there would reflow the layout
    on each token.  We deliberately keep the streaming widget content
    as a plain string and only swap to Markdown on finalize().
    """
    bubble = AlexBubble()
    async with _BubbleHarness(bubble).run_test() as pilot:
        bubble.set_response("# heading\n\npartial")
        await pilot.pause()
        renderable = _stored_renderable(_response_static(bubble))
        assert isinstance(renderable, str)


@pytest.mark.asyncio
async def test_insert_tool_renders_committed_prefix_as_markdown(markdown_on):
    """When a tool call commits in-flight assistant text as a prefix,
    that prefix must be rendered as Markdown so things like inline
    code survive the hand-off.
    """
    bubble = AlexBubble()
    async with _BubbleHarness(bubble).run_test() as pilot:
        bubble.set_response("Calling `time` next.")
        bubble.insert_tool("time", {"timezone": "UTC"})
        await pilot.pause()

        children = list(bubble.children)
        prefix = next(
            c for c in children if "response-prefix" in getattr(c, "classes", set())
        )
        assert isinstance(prefix, Static)
        assert isinstance(_stored_renderable(prefix), Markdown)

        # And the active streaming Static is reset to plain text.
        active = next(
            c for c in children if "response-text" in getattr(c, "classes", set())
        )
        active_renderable = _stored_renderable(active)
        assert isinstance(active_renderable, str)
        assert active_renderable == ""

        # Tool bubble was inserted between them.
        tool_index = next(i for i, c in enumerate(children) if isinstance(c, ToolBubble))
        prefix_index = children.index(prefix)
        active_index = children.index(active)
        assert prefix_index < tool_index < active_index
