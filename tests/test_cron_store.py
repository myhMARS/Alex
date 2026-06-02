"""Unit tests for CronStore — durable cron job persistence."""

import json
import os
from pathlib import Path

import pytest

from alex.scheduler.cron_store import CronStore
from alex.scheduler.manager import CronJob


class TestCronStore:
    """Tests for CronStore persistence layer."""

    def test_persist_writes_durable_job_atomically(self, tmp_path: Path):
        store = CronStore(storage_dir=tmp_path)
        job = CronJob(
            id="job-001",
            session_id="sess-1",
            name="test job",
            cron="*/5 * * * *",
            prompt="hello",
            recurring=True,
            durable=True,
        )
        store.persist(job)
        path = tmp_path / "job-001.json"
        assert path.exists()

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["id"] == "job-001"
        assert data["prompt"] == "hello"

    def test_persist_skips_non_durable_job(self, tmp_path: Path):
        store = CronStore(storage_dir=tmp_path)
        job = CronJob(
            id="job-002",
            session_id="sess-1",
            name="ephemeral",
            cron="* * * * *",
            prompt="hi",
            recurring=False,
            durable=False,
        )
        store.persist(job)
        assert not (tmp_path / "job-002.json").exists()

    def test_delete_removes_file(self, tmp_path: Path):
        store = CronStore(storage_dir=tmp_path)
        job = CronJob(
            id="job-003",
            session_id="sess-1",
            name="to delete",
            cron="0 9 * * *",
            prompt="x",
            durable=True,
        )
        store.persist(job)
        assert (tmp_path / "job-003.json").exists()

        store.delete("job-003")
        assert not (tmp_path / "job-003.json").exists()

    def test_delete_nonexistent_is_noop(self, tmp_path: Path):
        store = CronStore(storage_dir=tmp_path)
        store.delete("nonexistent")  # should not raise

    def test_restore_all_returns_valid_jobs(self, tmp_path: Path):
        store = CronStore(storage_dir=tmp_path)
        j1 = CronJob(
            id="r1", session_id="s", name="a", cron="* * * * *",
            prompt="p1", recurring=True, durable=True,
        )
        j2 = CronJob(
            id="r2", session_id="s", name="b", cron="0 9 * * 1",
            prompt="p2", recurring=False, durable=True, runs_done=0,
        )
        store.persist(j1)
        store.persist(j2)

        restored = store.restore_all()
        ids = {j.id for j in restored}
        assert "r1" in ids
        assert "r2" in ids

    def test_restore_all_skips_completed_one_shot(self, tmp_path: Path):
        store = CronStore(storage_dir=tmp_path)
        job = CronJob(
            id="done-1", session_id="s", name="done", cron="* * * * *",
            prompt="x", recurring=False, durable=True, runs_done=1,
        )
        store.persist(job)
        restored = store.restore_all()
        assert not any(j.id == "done-1" for j in restored)
        assert not (tmp_path / "done-1.json").exists()

    def test_restore_all_resets_running_status(self, tmp_path: Path):
        store = CronStore(storage_dir=tmp_path)
        job = CronJob(
            id="stuck", session_id="s", name="stuck", cron="*/5 * * * *",
            prompt="x", recurring=True, durable=True, status="RUNNING",
        )
        store.persist(job)
        restored = store.restore_all()
        stuck = next(j for j in restored if j.id == "stuck")
        assert stuck.status == "SCHEDULED"

    def test_restore_all_empty_dir(self, tmp_path: Path):
        store = CronStore(storage_dir=tmp_path)
        assert store.restore_all() == []

    def test_restore_all_nonexistent_dir(self, tmp_path: Path):
        store = CronStore(storage_dir=tmp_path / "nonexistent")
        assert store.restore_all() == []

    def test_persist_atomic_write_leaves_no_temp_file(self, tmp_path: Path):
        store = CronStore(storage_dir=tmp_path)
        job = CronJob(
            id="atomic", session_id="s", name="atomic", cron="* * * * *",
            prompt="test", durable=True,
        )
        store.persist(job)
        # No .tmp files left behind
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_persist_handles_write_failure_gracefully(self, tmp_path: Path, monkeypatch):
        store = CronStore(storage_dir=tmp_path)
        job = CronJob(
            id="fail", session_id="s", name="fail", cron="* * * * *",
            prompt="test", durable=True,
        )

        # Make os.fdopen fail after mkstemp succeeds
        original_fdopen = os.fdopen
        def _failing_fdopen(fd, *args, **kwargs):
            os.close(fd)
            raise OSError("disk full")

        monkeypatch.setattr("alex.scheduler.cron_store.os.fdopen", _failing_fdopen)

        with pytest.raises(OSError, match="disk full"):
            store.persist(job)

        # Ensure no .tmp file leaked
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_persist_job_with_special_characters(self, tmp_path: Path):
        store = CronStore(storage_dir=tmp_path)
        job = CronJob(
            id="unicode-🐛",
            session_id="sess",
            name="测试任务",
            cron="*/5 * * * *",
            prompt="你好世界",
            recurring=True,
            durable=True,
            last_error="some\nerror",
        )
        store.persist(job)
        path = tmp_path / "unicode-🐛.json"
        assert path.exists()

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["name"] == "测试任务"
        assert data["prompt"] == "你好世界"
