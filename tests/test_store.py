"""Tests for SessionPersistence — save/load/list/delete through store adapter."""

import os
import threading
import time

import pytest

from alex import messages as msg
from alex.store.session_adapter import SessionPersistence
from alex.store.session import SESSIONS_DIR


class TestSessionPersistence:
    def test_save_and_load(self):
        sid = "test-save-load"
        messages = [msg.user_message("hello"), msg.assistant_message("hi")]
        try:
            SessionPersistence.save(sid, messages)
            bundle = SessionPersistence.load(sid)
            assert bundle is not None
            assert len(bundle["messages"]) == 2
            assert bundle["messages"][0]["content"] == "hello"
        finally:
            SessionPersistence.delete(sid)

    def test_load_nonexistent(self):
        assert SessionPersistence.load("nonexistent-session-12345") is None

    def test_save_and_list(self):
        sid = "test-list-session"
        messages = [msg.user_message("list test")]
        try:
            SessionPersistence.save(sid, messages)
            sessions = SessionPersistence.list_sessions()
            sids = [s["session_id"] for s in sessions]
            assert sid in sids
        finally:
            SessionPersistence.delete(sid)

    def test_delete(self):
        sid = "test-delete-session"
        messages = [msg.user_message("delete me")]
        SessionPersistence.save(sid, messages)
        assert SessionPersistence.load(sid) is not None
        assert SessionPersistence.delete(sid) is True
        assert SessionPersistence.load(sid) is None

    def test_delete_nonexistent(self):
        assert SessionPersistence.delete("nonexistent-xyz") is False

    def test_save_uses_atomic_replace(self, monkeypatch):
        sid = "test-atomic-save"
        messages = [msg.user_message("atomic")]
        replace_calls: list[tuple[str, str]] = []
        original_replace = os.replace

        def _replace(src: str, dst: str) -> None:
            replace_calls.append((str(src), str(dst)))
            original_replace(src, dst)

        monkeypatch.setattr("alex.store.session.os.replace", _replace)
        try:
            SessionPersistence.save(sid, messages)
            assert replace_calls
            target = str(SESSIONS_DIR / f"{sid}.json")
            assert target in {dst for _, dst in replace_calls}
        finally:
            SessionPersistence.delete(sid)

    def test_append_cron_record(self):
        sid = "test-cron-append"
        messages = [msg.user_message("cron test")]
        try:
            SessionPersistence.save(sid, messages)
            record = {
                "execution_id": "exec-1",
                "job_id": "job-1",
                "name": "test job",
                "status": "SUCCESS",
                "result": "done",
            }
            SessionPersistence.append_cron_record(sid, record)
            bundle = SessionPersistence.load(sid)
            cron = bundle.get("cron_history", [])
            assert len(cron) == 1
            assert cron[0]["execution_id"] == "exec-1"
        finally:
            SessionPersistence.delete(sid)

    def test_append_cron_record_deduplicate(self):
        sid = "test-cron-dedup"
        messages = [msg.user_message("cron dedup")]
        try:
            SessionPersistence.save(sid, messages)
            record = {"execution_id": "exec-1", "job_id": "j1", "name": "j", "status": "SUCCESS"}
            SessionPersistence.append_cron_record(sid, record)
            SessionPersistence.append_cron_record(sid, record)
            bundle = SessionPersistence.load(sid)
            assert len(bundle.get("cron_history", [])) == 1
        finally:
            SessionPersistence.delete(sid)

    def test_save_preserves_existing_cron_history(self):
        sid = "test-save-preserve-cron-history"
        try:
            SessionPersistence.save(sid, [msg.user_message("first")])
            SessionPersistence.append_cron_record(sid, {
                "execution_id": "exec-1",
                "job_id": "job-1",
                "name": "job",
                "status": "SUCCESS",
            })
            SessionPersistence.save(sid, [msg.user_message("second")])
            bundle = SessionPersistence.load(sid)
            cron = bundle.get("cron_history", [])
            assert len(cron) == 1
            assert cron[0]["execution_id"] == "exec-1"
        finally:
            SessionPersistence.delete(sid)

    def test_append_cron_record_serializes_same_session_writes(self, monkeypatch):
        sid = "test-cron-serialized-writes"
        original_atomic_write = __import__("alex.store.session", fromlist=["_atomic_write_json"])._atomic_write_json

        def _slow_atomic_write(path, data):
            time.sleep(0.05)
            return original_atomic_write(path, data)

        monkeypatch.setattr("alex.store.session._atomic_write_json", _slow_atomic_write)
        try:
            SessionPersistence.save(sid, [msg.user_message("base")])
            r1 = {"execution_id": "exec-1", "job_id": "job-1", "name": "job1", "status": "SUCCESS"}
            r2 = {"execution_id": "exec-2", "job_id": "job-2", "name": "job2", "status": "SUCCESS"}
            t1 = threading.Thread(target=SessionPersistence.append_cron_record, args=(sid, r1))
            t2 = threading.Thread(target=SessionPersistence.append_cron_record, args=(sid, r2))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            bundle = SessionPersistence.load(sid)
            execution_ids = {item["execution_id"] for item in bundle.get("cron_history", [])}
            assert execution_ids == {"exec-1", "exec-2"}
        finally:
            SessionPersistence.delete(sid)

    @pytest.mark.asyncio
    async def test_subscribe_handles_turn_completed(self):
        from alex.bus import AsyncEventBus
        from alex.bus.events import TurnCompleted

        sid = "test-subscribe-turn"
        messages = [msg.user_message("bus turn"), msg.assistant_message("ok")]
        bus = None
        try:
            bus = AsyncEventBus()
            await bus.start()
            await SessionPersistence.subscribe(bus)

            bus.publish(TurnCompleted(
                session_id=sid, turn_id="t1", source="agent",
                kind="user", messages=messages, content="ok",
            ))
            import asyncio
            await asyncio.sleep(0.1)

            bundle = SessionPersistence.load(sid)
            assert bundle is not None
            assert len(bundle["messages"]) == 2
        finally:
            if bus is not None:
                await bus.shutdown()
            SessionPersistence.delete(sid)

    @pytest.mark.asyncio
    async def test_subscribe_handles_cron_job_event(self):
        from alex.bus import AsyncEventBus
        from alex.bus.events import CronJobEvent

        sid = "test-subscribe-cron"
        messages = [msg.user_message("cron bus")]
        bus = None
        try:
            SessionPersistence.save(sid, messages)

            bus = AsyncEventBus()
            await bus.start()
            await SessionPersistence.subscribe(bus)

            bus.publish(CronJobEvent(
                session_id=sid, job_id="j1", name="test job",
                status="SUCCESS", prompt="test prompt", runs_done=1,
                result="ok", tool_call_id="exec-bus",
            ))
            import asyncio
            await asyncio.sleep(0.1)

            bundle = SessionPersistence.load(sid)
            cron = bundle.get("cron_history", [])
            assert len(cron) == 1
            assert cron[0]["execution_id"] == "exec-bus"
        finally:
            if bus is not None:
                await bus.shutdown()
            SessionPersistence.delete(sid)
