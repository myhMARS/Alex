"""Tests for SessionPersistence — save/load/list/delete through store adapter."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage, AIMessage

from alex.store.session_adapter import SessionPersistence
from alex.store.session import SESSIONS_DIR


class TestSessionPersistence:
    def test_save_and_load(self):
        sid = "test-save-load"
        messages = [HumanMessage(content="hello"), AIMessage(content="hi")]
        try:
            SessionPersistence.save(sid, messages)
            bundle = SessionPersistence.load(sid)
            assert bundle is not None
            assert len(bundle["messages"]) == 2
            assert bundle["messages"][0].content == "hello"
        finally:
            SessionPersistence.delete(sid)

    def test_load_nonexistent(self):
        assert SessionPersistence.load("nonexistent-session-12345") is None

    def test_save_and_list(self):
        sid = "test-list-session"
        messages = [HumanMessage(content="list test")]
        try:
            SessionPersistence.save(sid, messages)
            sessions = SessionPersistence.list_sessions()
            sids = [s["session_id"] for s in sessions]
            assert sid in sids
        finally:
            SessionPersistence.delete(sid)

    def test_delete(self):
        sid = "test-delete-session"
        messages = [HumanMessage(content="delete me")]
        SessionPersistence.save(sid, messages)
        assert SessionPersistence.load(sid) is not None
        assert SessionPersistence.delete(sid) is True
        assert SessionPersistence.load(sid) is None

    def test_delete_nonexistent(self):
        assert SessionPersistence.delete("nonexistent-xyz") is False

    def test_append_cron_record(self):
        sid = "test-cron-append"
        messages = [HumanMessage(content="cron test")]
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
        messages = [HumanMessage(content="cron dedup")]
        try:
            SessionPersistence.save(sid, messages)
            record = {"execution_id": "exec-1", "job_id": "j1", "name": "j", "status": "SUCCESS"}
            SessionPersistence.append_cron_record(sid, record)
            SessionPersistence.append_cron_record(sid, record)
            bundle = SessionPersistence.load(sid)
            assert len(bundle.get("cron_history", [])) == 1
        finally:
            SessionPersistence.delete(sid)

    @pytest.mark.asyncio
    async def test_subscribe_handles_turn_completed(self):
        from alex.bus import AsyncEventBus
        from alex.bus.events import TurnCompleted

        sid = "test-subscribe-turn"
        messages = [HumanMessage(content="bus turn"), AIMessage(content="ok")]
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
            await bus.shutdown()
            SessionPersistence.delete(sid)

    @pytest.mark.asyncio
    async def test_subscribe_handles_cron_job_event(self):
        from alex.bus import AsyncEventBus
        from alex.bus.events import CronJobEvent

        sid = "test-subscribe-cron"
        messages = [HumanMessage(content="cron bus")]
        try:
            SessionPersistence.save(sid, messages)

            bus = AsyncEventBus()
            await bus.start()
            await SessionPersistence.subscribe(bus)

            bus.publish(CronJobEvent(
                session_id=sid, job_id="j1", name="test job",
                status="SUCCESS", action="notify", runs_done=1,
                result="ok", tool_call_id="exec-bus",
            ))
            import asyncio
            await asyncio.sleep(0.1)

            bundle = SessionPersistence.load(sid)
            cron = bundle.get("cron_history", [])
            assert len(cron) == 1
            assert cron[0]["execution_id"] == "exec-bus"
        finally:
            await bus.shutdown()
            SessionPersistence.delete(sid)
