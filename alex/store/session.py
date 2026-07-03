"""Core session persistence — message sequence serialization.

Session files are stored as JSON arrays of serialized messages under
~/.alex/sessions/<session_id>.json.  Messages are plain dicts with
``role`` and ``content`` keys, directly JSON-compatible.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from alex import messages as msg

SESSIONS_DIR = Path.home() / ".alex" / "sessions"
_SESSION_LOCKS: dict[str, threading.RLock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()


# ── serialization (plain dicts → JSON) ────────────────────────────────────


from alex.messages import deserialize_message, serialize_message


def serialize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [serialize_message(m) for m in messages]


def deserialize_messages(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [deserialize_message(r) for r in records]


# ── file I/O ──────────────────────────────────────────────────────────────


def _session_path(session_id: str) -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR / f"{session_id}.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(prefix=".alex.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _get_session_lock(session_id: str) -> threading.RLock:
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = threading.RLock()
            _SESSION_LOCKS[session_id] = lock
        return lock


def save_session(session_id: str, messages: list[dict[str, Any]]) -> Path:
    """Persist a message sequence to ~/.alex/sessions/<session_id>.json."""
    return save_session_bundle(session_id, messages)


def save_session_bundle(
    session_id: str,
    messages: list[dict[str, Any]],
    cron_history: list[dict[str, Any]] | None = None,
) -> Path:
    """Persist messages plus session-scoped cron execution history."""
    with _get_session_lock(session_id):
        path = _session_path(session_id)
        existing = load_session_raw(session_id) or {}
        created_at = existing.get("created_at") or datetime.now().isoformat()
        first_msg = ""
        for m in messages:
            if msg.is_user(m):
                first_msg = m.get("content", "")
                break
        if not first_msg and messages:
            first_msg = messages[0].get("content", "")
        if first_msg and len(first_msg) > 80:
            first_msg = first_msg[:80]
        effective_cron_history = cron_history
        if effective_cron_history is None:
            effective_cron_history = list(existing.get("cron_history", []) or [])
        data: dict[str, Any] = {
            "session_id": session_id,
            "created_at": created_at,
            "first_message": first_msg,
            "messages": serialize_messages(messages),
            "cron_history": list(effective_cron_history),
        }
        _atomic_write_json(path, data)
        return path


def load_session(session_id: str) -> list[dict[str, Any]] | None:
    """Load a message sequence from disk.  Returns None if the file is missing or corrupt."""
    path = _session_path(session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return deserialize_messages(data.get("messages", []))
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def load_session_bundle(session_id: str) -> dict[str, Any] | None:
    """Load messages and session-scoped cron execution history."""
    path = _session_path(session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "session_id": data.get("session_id", session_id),
            "created_at": data.get("created_at", ""),
            "first_message": data.get("first_message", ""),
            "messages": deserialize_messages(data.get("messages", [])),
            "cron_history": list(data.get("cron_history", []) or []),
        }
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def append_cron_history(session_id: str, record: dict[str, Any]) -> Path | None:
    """Append one completed cron execution record to a session file."""
    with _get_session_lock(session_id):
        bundle = load_session_bundle(session_id)
        if bundle is None:
            return None
        history = list(bundle.get("cron_history", []) or [])
        execution_id = str(record.get("execution_id", ""))
        if execution_id and any(str(item.get("execution_id", "")) == execution_id for item in history):
            return _session_path(session_id)
        history.append(record)
        return save_session_bundle(session_id, bundle["messages"], history)


def load_session_raw(session_id: str) -> dict[str, Any] | None:
    """Return the raw JSON dict for a session (used by TUI for metadata display)."""
    path = _session_path(session_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return None


@dataclass
class SessionMeta:
    """Lightweight metadata for the session picker."""
    session_id: str
    created_at: str
    first_message: str
    message_count: int


def list_sessions() -> list[SessionMeta]:
    """List saved sessions, newest first."""
    sessions: list[SessionMeta] = []
    if not SESSIONS_DIR.exists():
        return sessions
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            msgs = data.get("messages", [])
            sessions.append(SessionMeta(
                session_id=data.get("session_id", f.stem),
                created_at=data.get("created_at", ""),
                first_message=data.get("first_message", "")[:20],
                message_count=len(msgs),
            ))
        except (json.JSONDecodeError, TypeError):
            continue
    sessions.sort(key=lambda s: s.created_at, reverse=True)
    return sessions


def delete_session(session_id: str) -> bool:
    """Delete a session file.  Returns True if the file existed."""
    with _get_session_lock(session_id):
        path = _session_path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False
