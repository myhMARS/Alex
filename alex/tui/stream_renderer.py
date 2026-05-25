"""StreamRenderer — shared rendering state for user and cron turns.

Manages bubble lifecycle, token/thinking collection, tool-call tracking,
and turn finalization.  Both the async-generator path (user turns) and
the event-bus path (cron turns) delegate to a StreamRenderer instance.
"""

from __future__ import annotations

from alex.tui.view_models import ChatTurn
from alex.tui.presenter import AlexBubble, ToolBubble


class StreamRenderer:
    """Per-stream state holder — one instance per in-flight turn.

    Callers push typed events (thinking, token, tool_started, …) and
    pull the final ChatTurn via ``build_turn()``.
    """

    def __init__(self, bubble: AlexBubble) -> None:
        self.bubble = bubble
        self.collected = ""
        self.thinking = ""
        self.skills: list[dict] = []
        self.tool_calls: list[dict] = []
        self._inflight_tools: dict[str, dict] = {}
        self._inflight_bubbles: dict[str, ToolBubble] = {}
        self._inflight_order: list[str] = []
        self.message_batch: list | None = None

    # ── event handlers ──────────────────────────────────────────────────

    def on_thinking(self, delta: str) -> None:
        self.thinking += delta

    def on_token(self, delta: str) -> None:
        self.collected += delta
        self.bubble.set_response(self.collected)

    def on_skill_loaded(self, skill_name: str, skill_pattern: str) -> None:
        self.skills.append({"name": skill_name, "pattern": skill_pattern})

    def on_tool_started(self, tool_id: str, tool_name: str, tool_input: dict | None = None) -> None:
        args = tool_input if isinstance(tool_input, dict) else {"input": str(tool_input or "")}
        self._inflight_tools[tool_id] = {"id": tool_id, "name": tool_name, "args": args, "output": ""}
        self._inflight_order.append(tool_id)
        self._inflight_bubbles[tool_id] = self.bubble.insert_tool(tool_name, args)

    def on_tool_finished(self, tool_id: str, output: str = "") -> None:
        tid = tool_id or ""
        if not tid or tid not in self._inflight_tools:
            while self._inflight_order and self._inflight_order[-1] not in self._inflight_tools:
                self._inflight_order.pop()
            tid = self._inflight_order.pop() if self._inflight_order else ""
        if tid and tid in self._inflight_tools:
            output_str = str(output or "")
            self._inflight_tools[tid]["output"] = output_str
            self.tool_calls.append(self._inflight_tools.pop(tid))
            try:
                self._inflight_order.remove(tid)
            except ValueError:
                pass
        tb = self._inflight_bubbles.pop(tid, None) if tid else None
        if tb:
            tb.set_done(output_str)

    def on_batch(self, messages: list) -> None:
        self.message_batch = messages

    # ── finalization ────────────────────────────────────────────────────

    def build_turn(self, user_input: str = "", *, kind: str = "user") -> ChatTurn:
        return ChatTurn(
            user_input=user_input,
            response=self.collected,
            thinking=self.thinking,
            tool_calls=list(self.tool_calls),
            skills=list(self.skills),
            kind=kind,
        )

    def finalize(self, turn: ChatTurn) -> None:
        self.bubble.finalize(turn)
