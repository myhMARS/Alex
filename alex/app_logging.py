"""Application logging setup for Alex."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from alex.config import get_log_backup_count, get_log_max_bytes

DEFAULT_LOG_DIR = Path.home() / ".alex" / "logs"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s %(levelname)-5s [%(module_tag)s] %(message)s"


class _ModuleTagFilter(logging.Filter):
    """为日志记录添加简短的模块标签，如 [agent] [bus] [tools]。"""

    _TAG_MAP = {
        "alex.agent": "agent",
        "alex.tools": "tools",
        "alex.bus": "bus",
        "alex.tui": "tui",
        "alex.mcp": "mcp",
        "alex.skill": "skill",
        "alex.memory": "memory",
        "alex.scheduler": "cron",
        "alex.store": "store",
        "alex.kernel": "kernel",
        "alex.llm": "llm",
        "alex.entry": "entry",
    }

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        tag = "alex"
        for prefix, short in self._TAG_MAP.items():
            if name.startswith(prefix):
                tag = short
                break
        record.module_tag = tag  # type: ignore[attr-defined]
        return True


def configure_logging(
    log_dir: Path | None = None,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> Path:
    """Configure Alex logging with rotating file logs only."""
    target_dir = log_dir or DEFAULT_LOG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / "alex.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for handler in list(root.handlers):
        if getattr(handler, "_alex_managed", False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    formatter = logging.Formatter(LOG_FORMAT)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes or get_log_max_bytes(DEFAULT_MAX_BYTES),
        backupCount=backup_count or get_log_backup_count(DEFAULT_BACKUP_COUNT),
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    file_handler.addFilter(_ModuleTagFilter())
    file_handler._alex_managed = True  # type: ignore[attr-defined]

    root.addHandler(file_handler)
    return log_path
