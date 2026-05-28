"""Modal screen for tool permission confirmation.

Used by :class:`alex.tui.notification_controller.NotificationController`
to satisfy ``PermissionPolicy.confirm_hook`` requests.

The modal renders a :class:`alex.tools.permissions.ToolApprovalRequest`
with a one-line summary and as many :class:`PreviewBlock` panels as the
caller produced (typically a unified diff for ``fs_write`` / ``edit`` or
a command preview for ``bash`` / ``pwsh``).  It returns one of three
outcomes:

- ``(True, False)``  — allow the current call only       (Y / A)
- ``(True, True)``   — allow and remember for the session (S)
- ``(False, False)`` — deny                              (N / Esc)
"""

from __future__ import annotations

from rich.syntax import Syntax
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from alex.tools.permissions import PreviewBlock, ToolApprovalRequest


class PermissionConfirmScreen(ModalScreen[tuple[bool, bool]]):
    """Modal that asks the user to grant a tool permission.

    Returns ``(granted, remember)``:

    - ``(True, False)`` for *Allow once* (Y or A)
    - ``(True, True)``  for *Allow for session* (S)
    - ``(False, False)`` for *Deny* (N or Esc)
    """

    DEFAULT_CSS = """
    PermissionConfirmScreen {
        align: center middle;
    }
    PermissionConfirmScreen > Vertical {
        width: 100;
        max-width: 95%;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    PermissionConfirmScreen .title {
        text-style: bold;
        color: $warning;
        margin: 0 0 1 0;
    }
    PermissionConfirmScreen .summary {
        margin: 0 0 1 0;
        height: auto;
    }
    PermissionConfirmScreen .preview-scroll {
        height: auto;
        max-height: 24;
        margin: 0 0 1 0;
    }
    PermissionConfirmScreen .preview-block {
        margin: 0 0 1 0;
        padding: 0 1;
        border: solid $panel;
        border-title-color: $text-muted;
        border-title-style: bold;
        height: auto;
    }
    PermissionConfirmScreen .keys-divider {
        color: $text-muted;
        margin: 1 0 0 0;
        height: 1;
    }
    PermissionConfirmScreen .keys {
        margin: 0;
        height: 1;
    }
    """

    BINDINGS = [
        Binding("y", "allow_once", "Allow once", show=False, priority=True),
        Binding("a", "allow_once", "Allow once", show=False, priority=True),
        Binding("s", "allow_always", "Allow for session", show=False, priority=True),
        Binding("n", "deny", "Deny", show=False, priority=True),
        Binding("escape", "deny", "Deny", show=False, priority=True),
    ]

    def __init__(self, request: ToolApprovalRequest) -> None:
        super().__init__()
        self._request = request

    def compose(self) -> ComposeResult:
        req = self._request
        title = f"⚠  {req.tool_name} requests permission '{req.permission}'"
        summary = req.summary or "(no preview available)"

        preview_children: list[Static] = [
            self._render_block(block) for block in req.preview
        ]
        preview_scroll = VerticalScroll(*preview_children, classes="preview-scroll")
        container = Vertical(
            Static(title, classes="title"),
            Static(summary, classes="summary"),
            preview_scroll,
            Static("─" * 96, classes="keys-divider"),
            Static(_build_keys_footer(), classes="keys"),
        )
        yield container

    @staticmethod
    def _render_block(block: PreviewBlock) -> Static:
        if block.kind == "diff":
            renderable = Syntax(
                block.body, "diff",
                theme="ansi_dark", word_wrap=False, background_color="default",
            )
        elif block.kind == "code":
            renderable = Text(block.body)
        else:
            renderable = Text(block.body)
        widget = Static(renderable, classes="preview-block")
        widget.border_title = block.title
        return widget

    def action_allow_once(self) -> None:
        self.dismiss((True, False))

    def action_allow_always(self) -> None:
        self.dismiss((True, True))

    def action_deny(self) -> None:
        self.dismiss((False, False))


# ── footer rendering ────────────────────────────────────────────────────


# Each tuple is ``(keys, label, style)`` for one action.  ``keys`` is the
# group of keys that triggers the action, ``label`` is the action name,
# and ``style`` is the Rich style applied to the key glyph so the eye
# locks onto the trigger key first.
_FOOTER_ACTIONS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("Y", "A"), "Allow once",         "bold green"),
    (("S",),     "Allow for session",  "bold yellow"),
    (("N", "Esc"), "Deny",             "bold red"),
)


def _build_keys_footer() -> Text:
    """Render the action footer as a Rich ``Text`` (no markup parsing).

    We avoid passing a string with ``[Y]``/``[N]`` to ``Static`` because
    Rich interprets ``[...]`` as a markup tag, swallowing the bracketed
    glyphs and leaving the user staring at slashes with no idea which
    key triggers which action.
    """
    text = Text(no_wrap=False, overflow="fold")
    text.append("Press ", style="default")
    for index, (keys, label, key_style) in enumerate(_FOOTER_ACTIONS):
        if index > 0:
            text.append("   ·   ", style="dim")
        for k_index, key in enumerate(keys):
            if k_index > 0:
                text.append("/", style="dim")
            text.append(key, style=key_style)
        text.append(" ", style="default")
        text.append(label, style="default")
    return text
