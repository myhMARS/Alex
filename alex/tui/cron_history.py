"""CronHistoryReadModel — in-memory cron execution history for a session.

Extracted from ChatHistory so the cron history read model is a
standalone object, not a timeline accessory field.
"""

from __future__ import annotations


class CronHistoryReadModel:
    """Per-session cron execution history — read model only.

    Updated by ChatProjector.persist_cron_record() when CronJobEvent
    fires with status SUCCESS or FAILED. Used for persisted execution
    records, not for the current `/cron` job list or the `cron_jobs`
    built-in tool.
    """

    def __init__(self) -> None:
        self._records: list[dict] = []

    @property
    def records(self) -> list[dict]:
        return self._records

    def add(self, record: dict) -> None:
        execution_id = str(record.get("execution_id", ""))
        if execution_id and any(
            str(r.get("execution_id", "")) == execution_id for r in self._records
        ):
            return
        self._records.append(record)

    def clear(self) -> None:
        self._records.clear()

    def restore(self, records: list[dict]) -> None:
        self._records = list(records or [])

    def query(self, q: str = "", limit: int = 20) -> list[dict]:
        records = list(self._records)
        q = (q or "").strip().lower()
        if q:
            records = [r for r in records if _record_matches(r, q)]
        records.sort(
            key=lambda r: float(r.get("finished_at") or r.get("started_at") or 0),
            reverse=True,
        )
        return records[: max(1, min(int(limit), 50))]


def _record_matches(rec: dict, q: str) -> bool:
    haystacks = [
        str(rec.get("execution_id", "")),
        str(rec.get("job_id", "")),
        str(rec.get("name", "")),
        str(rec.get("status", "")),
        str(rec.get("prompt", "")),
    ]
    return any(q in item.lower() for item in haystacks if item)
