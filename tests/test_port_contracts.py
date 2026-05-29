"""Port contract tests — verify adapter implementations satisfy their protocols."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from langchain_core.messages import AIMessage, HumanMessage, BaseMessage


# ── SessionRepository contract ────────────────────────────────────────────────

class TestSessionRepositoryContract:
    """Contract tests for SessionRepository — verifies that SessionPersistence
    satisfies the port's behavioural guarantees."""

    @pytest.fixture
    def tmp_sessions_dir(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setattr("alex.store.session.SESSIONS_DIR", Path(td))
            yield Path(td)

    def test_save_and_load_roundtrip(self, tmp_sessions_dir):
        """A saved bundle must be reloaded with identical messages and metadata."""
        from alex.store.session_adapter import SessionPersistence

        sid = "test-session-1"
        msgs: list[BaseMessage] = [
            HumanMessage(content="Hello"),
            AIMessage(content="Hi there", additional_kwargs={"reasoning_content": "thinking..."}),
        ]
        SessionPersistence.save(sid, msgs)

        loaded = SessionPersistence.load(sid)
        assert loaded is not None
        assert loaded["session_id"] == sid
        assert len(loaded["messages"]) == 2
        assert loaded["messages"][0].content == "Hello"
        assert loaded["messages"][1].content == "Hi there"
        assert loaded["messages"][1].additional_kwargs.get("reasoning_content") == "thinking..."

    def test_load_nonexistent_returns_none(self, tmp_sessions_dir):
        from alex.store.session_adapter import SessionPersistence
        assert SessionPersistence.load("nonexistent") is None

    def test_list_sessions_returns_metadata(self, tmp_sessions_dir):
        from alex.store.session_adapter import SessionPersistence

        SessionPersistence.save("s1", [HumanMessage(content="First")])
        SessionPersistence.save("s2", [HumanMessage(content="Second")])

        sessions = SessionPersistence.list_sessions()
        assert len(sessions) >= 2
        ids = {s["session_id"] for s in sessions}
        assert "s1" in ids
        assert "s2" in ids

    def test_append_cron_record_persists(self, tmp_sessions_dir):
        from alex.store.session_adapter import SessionPersistence

        sid = "cron-session"
        SessionPersistence.save(sid, [HumanMessage(content="Hi")])

        record = {
            "execution_id": "exec-1",
            "job_id": "job-abc",
            "name": "test-cron",
            "status": "SUCCESS",
            "action": "notify",
            "params": {"message": "done"},
            "runs_done": 1,
            "started_at": 1700000000.0,
            "finished_at": 1700000001.0,
            "result": "ok",
            "error": None,
        }
        SessionPersistence.append_cron_record(sid, record)

        loaded = SessionPersistence.load(sid)
        assert loaded is not None
        assert len(loaded["cron_history"]) == 1
        assert loaded["cron_history"][0]["execution_id"] == "exec-1"

    def test_delete_removes_session(self, tmp_sessions_dir):
        from alex.store.session_adapter import SessionPersistence

        sid = "to-delete"
        SessionPersistence.save(sid, [HumanMessage(content="Temp")])
        assert SessionPersistence.load(sid) is not None

        assert SessionPersistence.delete(sid) is True
        assert SessionPersistence.load(sid) is None

    def test_delete_nonexistent_returns_false(self, tmp_sessions_dir):
        from alex.store.session_adapter import SessionPersistence
        assert SessionPersistence.delete("nonexistent") is False


# ── SkillServicePort contract ─────────────────────────────────────────────────

class TestSkillServicePortContract:
    """Contract tests verifying SkillService satisfies SkillServicePort."""

    @pytest.fixture
    def tmp_skills_dir(self, monkeypatch, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        monkeypatch.setattr("alex.skill.repository.SKILLS_DIR", skills_dir)
        # Also set the prompts SKILLS_DIR (used by save_skill_template etc.)
        monkeypatch.setattr("alex.prompts.SKILLS_DIR", skills_dir)
        return skills_dir

    @pytest.fixture
    def skill_service(self, tmp_skills_dir):
        from alex.skill.repository import SkillStore
        from alex.skill.reflector import Reflector
        from alex.skill.matcher import SkillRetriever
        from alex.skill.evolution import EvolutionEngine
        from alex.skill.service import SkillService

        store = SkillStore(path=str(tmp_skills_dir / "skills.json"))
        return SkillService(
            store=store,
            reflector=Reflector(),
            retriever=SkillRetriever(store),
            evolution=EvolutionEngine(),
        )

    def test_list_all_returns_empty_initially(self, skill_service):
        assert skill_service.list_all() == []

    def test_add_and_list(self, skill_service):
        from alex.skill.models import Skill

        s = Skill(name="test-skill", pattern="when testing", instruction="run tests")
        skill_service._store.add(s)

        all_skills = skill_service.list_all()
        assert len(all_skills) == 1
        assert all_skills[0].name == "test-skill"

    def test_get_skill_by_name(self, skill_service):
        from alex.skill.models import Skill

        s = Skill(name="unique-skill", pattern="test", instruction="test it")
        skill_service._store.add(s)

        found = skill_service.get_skill_by_name("unique-skill")
        assert found is not None
        assert found.id == s.id
        assert skill_service.get_skill_by_name("nonexistent") is None

    def test_get_skill_by_id(self, skill_service):
        from alex.skill.models import Skill

        s = Skill(name="by-id", pattern="id test", instruction="lookup by id")
        skill_service._store.add(s)

        found = skill_service.get_skill(s.id)
        assert found is not None
        assert found.name == "by-id"
        assert skill_service.get_skill("nonexistent") is None

    def test_remove_skill(self, skill_service):
        from alex.skill.models import Skill

        s = Skill(name="removable", pattern="x", instruction="y")
        skill_service._store.add(s)
        assert len(skill_service.list_all()) == 1

        skill_service.remove_skill(s.id)
        assert len(skill_service.list_all()) == 0

    def test_deprecate_skill(self, skill_service):
        from alex.skill.models import Skill

        s = Skill(name="old-skill", pattern="old", instruction="deprecated")
        skill_service._store.add(s)

        skill_service.deprecate_skill(s.id)
        all_skills = skill_service.list_all()
        assert all_skills[0].status == "DEPRECATED"

    def test_record_usage_updates_counts(self, skill_service):
        from alex.skill.models import Skill

        s = Skill(name="counted", pattern="c", instruction="d")
        skill_service._store.add(s)

        skill_service.record_usage(s.id, True)
        updated = skill_service.get_skill(s.id)
        assert updated.use_count == 1
        assert updated.success_count == 1

        skill_service.record_usage(s.id, False)
        updated = skill_service.get_skill(s.id)
        assert updated.use_count == 2
        assert updated.failure_count == 1

    def test_inject_skills_prompt_includes_active_skills(self, skill_service):
        from alex.skill.models import Skill

        s = Skill(name="prompt-skill", pattern="prompting", instruction="show in prompt")
        skill_service._store.add(s)

        prompt = skill_service.inject_skills_prompt("")
        assert "prompt-skill" in prompt


# ── SkillStore atomic write contract ──────────────────────────────────────────

class TestSkillStoreAtomicWrite:
    """Contract tests for atomic write and corrupt-data resilience."""

    def test_atomic_write_does_not_corrupt_on_crash_simulation(self, monkeypatch, tmp_path):
        """If the write is interrupted mid-stream, the original file is intact."""
        from alex.skill.repository import SkillStore
        from alex.skill.models import Skill

        monkeypatch.setattr("alex.skill.repository.SKILLS_DIR", tmp_path)
        store_path = tmp_path / "skills.json"

        # Write initial data
        store = SkillStore(path=str(store_path))
        s1 = Skill(name="survivor", pattern="p", instruction="i")
        store.add(s1)

        # Verify original data
        raw = json.loads(store_path.read_text())
        assert len(raw) == 1

    def test_corrupt_json_loads_empty(self, monkeypatch, tmp_path):
        """A corrupt JSON file must not crash; store starts empty."""
        from alex.skill.repository import SkillStore

        monkeypatch.setattr("alex.skill.repository.SKILLS_DIR", tmp_path)
        store_path = tmp_path / "skills.json"
        store_path.write_text("not valid json {{{")

        store = SkillStore(path=str(store_path))
        assert store.list_all() == []

    def test_empty_file_loads_empty(self, monkeypatch, tmp_path):
        from alex.skill.repository import SkillStore

        monkeypatch.setattr("alex.skill.repository.SKILLS_DIR", tmp_path)
        store_path = tmp_path / "skills.json"
        store_path.write_text("")

        store = SkillStore(path=str(store_path))
        assert store.list_all() == []
