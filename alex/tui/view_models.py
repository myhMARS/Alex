"""UI view models — pure data classes and transformation functions for the TUI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage


@dataclass
class ChatTurn:
    """One turn of conversation — UI view-model derived from message sequences."""
    user_input: str
    response: str = ""
    thinking: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    skills: list[dict] = field(default_factory=list)
    kind: str = "user"  # "user" | "cron" — controls whether render_turn shows a UserBubble


def _parse_load_skill_output(output: str) -> dict | None:
    """Extract {name, pattern} from a load_skill tool output string.

    The output format is:
        [Skill: <name>]
        When to apply: <pattern>
        Execution methodology:
        <instruction>
    """
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


def _messages_to_turns(messages: list[BaseMessage]) -> tuple[list[ChatTurn], list[BaseMessage]]:
    """Convert a message sequence to UI view-models.

    Uses tool_call_id → dict mapping so that multi-tool AIMessages are
    correctly paired with their ToolMessage outputs.  A single-pointer
    pending_tool would lose all but the last tool call per message.

    Returns (turns, messages) — messages pass through unchanged so
    Agent.restore_history() gets the exact sequence.
    """
    turns: list[ChatTurn] = []
    current: ChatTurn | None = None
    pending: dict[str, dict] = {}     # tool_call_id → tool_call dict
    _order: list[str] = []             # insertion order for fallback

    for msg in messages:
        if isinstance(msg, HumanMessage):
            if current is not None:
                turns.append(current)
            current = ChatTurn(user_input=str(msg.content), kind="user")
            pending.clear()
            _order.clear()

        elif isinstance(msg, AIMessage) and msg.tool_calls:
            ak = getattr(msg, "additional_kwargs", None)
            turn_start = bool(isinstance(ak, dict) and ak.get("alex_turn_start"))
            turn_kind = str(ak.get("alex_turn_kind", "cron")) if isinstance(ak, dict) else "cron"
            if turn_start and current is not None:
                turns.append(current)
                current = None
                pending.clear()
                _order.clear()
            if current is None:
                current = ChatTurn(user_input="", kind=turn_kind)
            prefix = str(msg.content) if msg.content else ""
            for tc in msg.tool_calls:
                tc_id = str(tc.get("id", ""))
                tc_dict = {
                    "name": tc.get("name", ""),
                    "args": tc.get("args", {}),
                    "id": tc_id,
                    "output": "",
                    "prefix": prefix,
                }
                current.tool_calls.append(tc_dict)
                if tc_id:
                    pending[tc_id] = tc_dict
                    _order.append(tc_id)

        elif isinstance(msg, ToolMessage):
            tc_id = str(getattr(msg, "tool_call_id", ""))
            matched = pending.pop(tc_id, None) if tc_id else None
            if matched is not None:
                matched["output"] = str(msg.content)
                if tc_id in _order:
                    _order.remove(tc_id)
                # Recover skill metadata from load_skill tool output
                if matched.get("name") == "load_skill":
                    skill_info = _parse_load_skill_output(str(msg.content))
                    if skill_info:
                        current.skills.append(skill_info)
            elif _order:
                # fallback: match oldest unmatched tool call
                fallback_id = _order.pop(0)
                fb = pending.pop(fallback_id, None)
                if fb is not None:
                    fb["output"] = str(msg.content)

        elif isinstance(msg, AIMessage) and not msg.tool_calls:
            if current is None:
                current = ChatTurn(user_input="", kind="cron")
            current.response = str(msg.content)
            ak = getattr(msg, "additional_kwargs", None)
            if ak and isinstance(ak, dict):
                current.thinking = ak.get("reasoning_content", "") or ""

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
        self._messages: list[BaseMessage] = []
        self._cron_history: list[dict] = []

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
    def loaded_messages(self) -> list[BaseMessage]:
        """The authoritative message sequence — for Agent.restore_history()."""
        return self._messages

    @property
    def cron_history(self) -> list[dict]:
        return self._cron_history

    def add(self, turn: ChatTurn, messages_delta: list[BaseMessage] | None = None) -> None:
        """Record a turn with its exact message delta from the Agent."""
        self._turns.append(turn)
        if messages_delta:
            self._messages.extend(messages_delta)

    def add_cron_record(self, record: dict) -> None:
        execution_id = str(record.get("execution_id", ""))
        if execution_id and any(str(item.get("execution_id", "")) == execution_id for item in self._cron_history):
            return
        self._cron_history.append(record)

    def clear(self) -> None:
        self._turns.clear()
        self._messages.clear()
        self._cron_history.clear()

    def restore_from_bundle(self, bundle: dict) -> None:
        """Restore ChatHistory state from a loaded session bundle."""
        self._turns, self._messages = _messages_to_turns(bundle.get("messages", []))
        self._cron_history = list(bundle.get("cron_history", []) or [])
        self._session_id = bundle.get("session_id", self._session_id)
