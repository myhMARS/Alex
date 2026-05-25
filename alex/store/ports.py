"""Store module public interfaces — aligned with SessionPersistence real API.

SessionRepository uses bundle semantics: load_bundle returns a full session
bundle (messages + cron_history + metadata), not just messages.
"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict

from langchain_core.messages import BaseMessage


class SessionBundle(TypedDict):
    session_id: str
    created_at: str
    first_message: str
    messages: list[BaseMessage]
    cron_history: list[dict[str, Any]]


class SessionRepository(Protocol):
    """Session persistence — bundle-based save/load with cron history.

    Matches the real SessionPersistence adapter API.
    """

    def save(self, session_id: str, messages: list[BaseMessage]) -> None: ...

    def load(self, session_id: str) -> dict[str, Any] | None: ...

    def list_sessions(self) -> list[dict[str, Any]]: ...

    def delete(self, session_id: str) -> bool: ...

    def append_cron_record(self, session_id: str, record: dict[str, Any]) -> None: ...

    @staticmethod
    async def subscribe(bus: Any) -> None: ...


class SkillRepository(Protocol):
    """Skill metadata persistence — CRUD for skill definitions."""

    async def list_all(self) -> list[Any]: ...

    async def get_by_name(self, name: str) -> Any | None: ...

    async def save(self, skill: Any) -> None: ...

    async def delete(self, skill_id: str) -> None: ...
