"""Tests for the permission confirm modal footer rendering.

The modal itself runs inside Textual; full keyboard interaction is
covered indirectly via the gating tests in ``test_permissions.py`` and
``test_approval_summariser.py``.  These tests pin the *visible* glyphs
of the footer so a future refactor can't accidentally let Rich's markup
parser swallow bracketed key labels again.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from alex.tui.confirm_screen import _FOOTER_ACTIONS, _build_keys_footer


class TestKeysFooter:
    def test_plain_text_contains_every_key_glyph(self):
        text = _build_keys_footer()
        plain = text.plain
        # Every action's key letters and label survive — they are not
        # consumed by Rich markup parsing.
        for keys, label, _style in _FOOTER_ACTIONS:
            for key in keys:
                assert key in plain, f"key {key!r} missing from {plain!r}"
            assert label in plain, f"label {label!r} missing from {plain!r}"

    def test_footer_explicitly_says_press(self):
        plain = _build_keys_footer().plain
        assert plain.startswith("Press ")

    def test_footer_separates_actions(self):
        plain = _build_keys_footer().plain
        # The separator marker between adjacent actions makes it clear
        # which keys belong to which label.
        assert "·" in plain or "  " in plain

    def test_keys_have_distinct_styles(self):
        """Each key span must carry its action's accent style.

        We rely on ``Text.spans`` rather than re-checking the plain
        string because the whole point of this test is to ensure the
        glyph survives with a visible style attached to it.
        """
        text = _build_keys_footer()
        styled_glyphs: dict[str, str] = {}
        for span in text.spans:
            chunk = text.plain[span.start: span.end]
            style = str(span.style)
            for keys, _label, key_style in _FOOTER_ACTIONS:
                for key in keys:
                    if chunk == key:
                        styled_glyphs[key] = style
                        assert key_style in style, (
                            f"key {key!r} expected style {key_style!r}, got {style!r}"
                        )

        # Every defined action key must appear at least once with a span.
        for keys, _label, _style in _FOOTER_ACTIONS:
            for key in keys:
                assert key in styled_glyphs, f"no styled span for {key!r}"
