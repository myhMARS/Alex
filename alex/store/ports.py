"""Store module public interfaces — stable repository contracts."""

from __future__ import annotations

from typing import Any, Protocol, TypedDict


class SessionBundle(TypedDict):
    """A complete session snapshot: messages + cron history + metadata."""

    session_id: str
    created_at: str
    first_message: str
    messages: list[dict[str, Any]]
    cron_history: list[dict[str, Any]]


class SessionRepository(Protocol):
    """Session persistence — load/save/list/delete session bundles."""

    def save(self, session_id: str, messages: list[dict[str, Any]]) -> None: ...

    def load(self, session_id: str) -> SessionBundle | None: ...

    def append_cron_record(self, session_id: str, record: dict[str, Any]) -> None: ...

    def list_sessions(self) -> list[dict[str, Any]]: ...

    def delete(self, session_id: str) -> bool: ...


class SkillRepository(Protocol):
    """Skill metadata persistence — CRUD for skill definitions."""

    def list_all(self) -> list[Any]: ...

    def get(self, skill_id: str) -> Any | None: ...

    def add(self, skill: Any) -> None: ...

    def update(self, skill: Any) -> None: ...

    def remove(self, skill_id: str) -> None: ...

    def deprecate(self, skill_id: str) -> None: ...
