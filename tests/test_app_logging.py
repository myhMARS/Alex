from __future__ import annotations

import logging

from alex.app_logging import configure_logging


def test_configure_logging_creates_log_directory_and_file(tmp_path):
    log_path = configure_logging(tmp_path)
    logger = logging.getLogger("alex.tests.logging")
    logger.info("hello log file")

    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_path == tmp_path / "alex.log"
    assert log_path.exists()
    assert "hello log file" in log_path.read_text(encoding="utf-8")


def test_configure_logging_rotates_files(tmp_path):
    log_path = configure_logging(tmp_path, max_bytes=200, backup_count=2)
    logger = logging.getLogger("alex.tests.rotation")

    for index in range(40):
        logger.info("rotation message %s %s", index, "x" * 40)

    for handler in logging.getLogger().handlers:
        handler.flush()

    rotated = tmp_path / "alex.log.1"
    assert log_path.exists()
    assert rotated.exists()


def test_configure_logging_replaces_previous_managed_handlers(tmp_path):
    root = logging.getLogger()
    first = configure_logging(tmp_path / "first")
    first_handlers = [h for h in root.handlers if getattr(h, "_alex_managed", False)]

    second = configure_logging(tmp_path / "second")
    second_handlers = [h for h in root.handlers if getattr(h, "_alex_managed", False)]

    assert first != second
    assert len(first_handlers) == 1
    assert len(second_handlers) == 1
    assert all(handler in root.handlers for handler in second_handlers)
    assert all(handler not in root.handlers for handler in first_handlers)


def test_configure_logging_does_not_add_stream_handler(tmp_path):
    root = logging.getLogger()
    configure_logging(tmp_path)
    managed_handlers = [h for h in root.handlers if getattr(h, "_alex_managed", False)]
    assert len(managed_handlers) == 1
    assert isinstance(managed_handlers[0], logging.FileHandler)
