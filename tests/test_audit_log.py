"""Tests for the AuditLogger and policy audit integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alex.tools.permissions import (
    AuditEvent,
    AuditLogger,
    PERMISSION_WRITE,
    PermissionPolicy,
    ToolApprovalRequest,
)


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "permissions.jsonl"


class TestAuditLogger:
    @pytest.mark.asyncio
    async def test_record_appends_jsonl(self, audit_path: Path):
        logger = AuditLogger(audit_path)
        await logger.record(AuditEvent(
            ts=1700000000.0,
            tool_name="fs_write",
            permission="write",
            decision="allow_once",
            args_digest="path=/tmp/x",
            reason="",
        ))
        await logger.record(AuditEvent(
            ts=1700000001.0,
            tool_name="shell_run",
            permission="shell",
            decision="deny",
            args_digest="argv=['rm', '-rf']",
            reason="user denied permission 'shell'",
        ))

        records = logger.read_all()
        assert len(records) == 2
        assert records[0]["tool"] == "fs_write"
        assert records[0]["decision"] == "allow_once"
        assert "iso" in records[0]
        assert records[1]["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_failure_silently_swallowed(self, tmp_path: Path):
        # Pointing at a directory we cannot write to — the record() call
        # must not raise even though the underlying write fails.
        bad_path = tmp_path / "ro" / "audit.jsonl"
        bad_path.parent.mkdir()
        # Make parent read-only on Unix-like systems; on Windows we just
        # ensure that pointing at the directory itself fails.
        logger = AuditLogger(bad_path)
        # Replace _write with a function that raises to simulate any
        # storage failure deterministically across platforms.

        def _boom(_event: AuditEvent) -> None:
            raise OSError("disk full")

        logger._write = _boom  # type: ignore[method-assign]
        await logger.record(AuditEvent(
            ts=0, tool_name="x", permission="write", decision="deny",
        ))


class TestPolicyAuditing:
    @pytest.mark.asyncio
    async def test_auto_allow_records_decision(self, audit_path: Path):
        logger = AuditLogger(audit_path)
        policy = PermissionPolicy(audit_logger=logger)
        # default: read is auto-allowed
        await policy.check("fs_read", "read")
        records = logger.read_all()
        assert len(records) == 1
        assert records[0]["decision"] == "auto_allow"
        assert records[0]["tool"] == "fs_read"

    @pytest.mark.asyncio
    async def test_auto_deny_records_decision(self, audit_path: Path):
        logger = AuditLogger(audit_path)
        policy = PermissionPolicy(audit_logger=logger)
        # default: write is not allowed and there's no hook
        granted, _ = await policy.check("fs_write", "write")
        assert not granted
        records = logger.read_all()
        assert len(records) == 1
        assert records[0]["decision"] == "auto_deny"

    @pytest.mark.asyncio
    async def test_allow_once_vs_always_recorded_distinctly(self, audit_path: Path):
        logger = AuditLogger(audit_path)

        async def _once(_req):
            return (True, False)

        policy_once = PermissionPolicy(confirm_hook=_once, audit_logger=logger)
        await policy_once.check("fs_write", "write")

        async def _always(_req):
            return (True, True)

        policy_always = PermissionPolicy(confirm_hook=_always, audit_logger=logger)
        await policy_always.check("fs_write", "write")

        decisions = [r["decision"] for r in logger.read_all()]
        assert decisions == ["allow_once", "allow_always"]

    @pytest.mark.asyncio
    async def test_summary_recorded_in_args_digest(self, audit_path: Path):
        logger = AuditLogger(audit_path)
        policy = PermissionPolicy(audit_logger=logger)
        request = ToolApprovalRequest(
            tool_name="fs_write",
            permission=PERMISSION_WRITE,
            args={"path": "/tmp/x", "content": "hello"},
            summary="Edit /tmp/x (+1 / -0, 5 bytes total)",
        )
        # default policy auto-denies write — but the digest should still be
        # recorded so an audit reviewer sees what was attempted.
        await policy.check_request(request)
        records = logger.read_all()
        assert len(records) == 1
        assert records[0]["args_digest"].startswith("Edit /tmp/x")
