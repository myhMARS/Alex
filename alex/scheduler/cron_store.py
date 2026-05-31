"""CronStore — durable cron job persistence to ~/.alex/cron/."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from alex.scheduler.manager import CRON_DIR, CronJob


class CronStore:
    """Atomic file-based persistence for durable cron jobs.

    Only jobs with ``durable=True`` are written to disk.  All writes use
    a temp-file + atomic-replace strategy to avoid corrupting the on-disk
    state on crash.
    """

    def __init__(self, storage_dir: Path | None = None) -> None:
        self._storage_dir = storage_dir or CRON_DIR

    # ── path helpers ──────────────────────────────────────────────────

    def _job_path(self, job_id: str) -> Path:
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        return self._storage_dir / f"{job_id}.json"

    # ── public API ─────────────────────────────────────────────────────

    def persist(self, job: CronJob) -> None:
        """Write *job* to disk atomically (no-op when ``job.durable`` is False)."""
        if not job.durable:
            return
        path = self._job_path(job.id)
        payload = json.dumps(job.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
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

    def delete(self, job_id: str) -> None:
        """Remove the on-disk file for *job_id* if it exists."""
        path = self._job_path(job_id)
        if path.exists():
            path.unlink()

    def restore_all(self) -> list[CronJob]:
        """Read all persisted jobs from disk.

        Returns only valid jobs.  One-shot jobs that have already run at
        least once are deleted from disk and skipped.
        Jobs stuck in RUNNING status are reset to SCHEDULED.
        """
        if not self._storage_dir.exists():
            return []
        jobs: list[CronJob] = []
        for path in sorted(self._storage_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = CronJob.from_dict(data)
            except Exception:
                continue
            if not job.id or not job.cron or not job.prompt:
                continue
            # Clean up one-shot jobs that already finished.
            if not job.recurring and job.runs_done > 0:
                self.delete(job.id)
                continue
            if job.status == "RUNNING":
                job.status = "SCHEDULED"
            jobs.append(job)
        return jobs
