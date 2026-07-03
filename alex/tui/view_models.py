"""UI view models — pure data classes and transformation functions for the TUI."""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from alex import messages as msg
from alex.tui.cron_history import CronHistoryReadModel


@dataclass
class ChatTurn:
    """One turn of conversation — UI view-model derived from message sequences."""
    user_input: str
    response: str = ""
    thinking: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    skills: list[dict] = field(default_factory=list)
    kind: str = "user"  # "user" | "cron" — controls whether render_turn shows a UserBubble
    is_error: bool = False  # True when the turn ended with an LLM or agent error


def _parse_load_skill_output(output: str) -> dict | None:
    """Extract {name, pattern} from a load_skill tool output string."""
    if not output.startswith("[Skill:"):
        return None
    lines = output.split("\n")
    name = lines[0].removeprefix("[Skill:").removesuffix("]").strip() if lines else ""
    pattern = ""
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.lower().startswith("when to apply:"):
            pattern = stripped.split(":", 1)[1].strip()
            break
    return {"name": name, "pattern": pattern} if name else None


def _messages_to_turns(messages: list[dict[str, Any]]) -> tuple[list[ChatTurn], list[dict[str, Any]]]:
    """Convert a message sequence to UI view-models.

    Returns (turns, messages) — messages pass through unchanged so
    Agent.restore_history() gets the exact sequence.
    """
    turns: list[ChatTurn] = []
    current: ChatTurn | None = None
    pending: dict[str, dict] = {}     # tool_call_id → tool_call dict
    _order: list[str] = []             # insertion order for fallback

    for m in messages:
        if msg.is_user(m):
            if current is not None:
                turns.append(current)
            turn_kind = str(m.get("alex_turn_kind", "user"))
            current = ChatTurn(user_input=str(m.get("content", "")), kind=turn_kind)
            pending.clear()
            _order.clear()

        elif msg.is_assistant(m) and msg.has_tool_calls(m):
            tool_calls_list = m.get("tool_calls", []) or []
            turn_start = m.get("alex_turn_start", False)
            turn_kind = str(m.get("alex_turn_kind", "cron"))
            if turn_start and current is not None:
                turns.append(current)
                current = None
                pending.clear()
                _order.clear()
            if current is None:
                current = ChatTurn(user_input="", kind=turn_kind)
            prefix = str(m.get("content", ""))
            for tc in tool_calls_list:
                tc_id = str(tc.get("id", ""))
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                # Parse JSON arguments string from OpenAI format
                args_raw = fn.get("arguments", "{}")
                if isinstance(args_raw, str):
                    try:
                        args = _json.loads(args_raw)
                    except (_json.JSONDecodeError, TypeError):
                        args = {}
                else:
                    args = args_raw if isinstance(args_raw, dict) else {}
                tc_dict = {
                    "name": fn.get("name", ""),
                    "args": args,
                    "id": tc_id,
                    "output": "",
                    "prefix": prefix,
                }
                current.tool_calls.append(tc_dict)
                if tc_id:
                    pending[tc_id] = tc_dict
                    _order.append(tc_id)

        elif msg.is_tool(m):
            tc_id = msg.get_tool_call_id(m)
            matched = pending.pop(tc_id, None) if tc_id else None
            if matched is not None:
                matched["output"] = str(m.get("content", ""))
                if tc_id in _order:
                    _order.remove(tc_id)
                if matched.get("name") == "load_skill":
                    skill_info = _parse_load_skill_output(str(m.get("content", "")))
                    if skill_info:
                        current.skills.append(skill_info)
            elif _order:
                fallback_id = _order.pop(0)
                fb = pending.pop(fallback_id, None)
                if fb is not None:
                    fb["output"] = str(m.get("content", ""))

        elif msg.is_assistant(m) and not msg.has_tool_calls(m):
            if current is None:
                current = ChatTurn(user_input="", kind="cron")
            current.response = str(m.get("content", ""))
            current.thinking = msg.get_reasoning(m)

    if current is not None:
        turns.append(current)
    return turns, messages


class ChatHistory:
    """UI-side session bookkeeping — view-model state only.

    Maintains a ChatTurn list for rendering and an authoritative message
    sequence for Agent.restore_history().  Persistence is handled by the
    store module via TurnCompleted bus events — ChatHistory never calls
    save directly.
    """

    def __init__(self, session_id: str | None = None) -> None:
        self._turns: list[ChatTurn] = []
        self._messages: list[dict[str, Any]] = []
        self._cron = CronHistoryReadModel()

        if session_id:
            self._session_id = session_id
        else:
            self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def turns(self) -> list[ChatTurn]:
        return self._turns

    @property
    def loaded_messages(self) -> list[dict[str, Any]]:
        """The authoritative message sequence — for Agent.restore_history()."""
        return self._messages

    @property
    def cron_history(self) -> list[dict]:
        return self._cron.records

    def add(self, turn: ChatTurn, messages_delta: list[dict[str, Any]] | None = None) -> None:
        """Record a turn with its exact message delta from the Agent."""
        self._turns.append(turn)
        if messages_delta:
            self._messages.extend(messages_delta)

    def add_cron_record(self, record: dict) -> None:
        self._cron.add(record)

    def clear(self) -> None:
        self._turns.clear()
        self._messages.clear()
        self._cron.clear()

    def restore_from_bundle(self, bundle: dict) -> None:
        """Restore ChatHistory state from a loaded session bundle."""
        self._turns, self._messages = _messages_to_turns(bundle.get("messages", []))
        self._cron.restore(bundle.get("cron_history", []) or [])
        self._session_id = bundle.get("session_id", self._session_id)
