"""Store module public interfaces."""

from __future__ import annotations

from typing import Any, Protocol

from langchain_core.messages import BaseMessage


class SessionStore(Protocol):
    """Session persistence — save/load BaseMessage sequences + cron history."""

    async def load(self, session_id: str) -> list[BaseMessage]: ...

    async def save(self, session_id: str, messages: list[BaseMessage]) -> None: ...

    async def save_cron_history(self, session_id: str, records: list[dict[str, Any]]) -> None: ...


class SkillRepository(Protocol):
    """Skill metadata persistence — CRUD for skill definitions."""

    async def list_all(self) -> list[Any]: ...

    async def get_by_name(self, name: str) -> Any | None: ...

    async def save(self, skill: Any) -> None: ...

    async def delete(self, skill_id: str) -> None: ...
