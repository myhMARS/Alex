"""Tool permission policy — fail-closed, environment-driven.

Every tool that can have side effects declares a required permission via
``StructuredTool.metadata["required_permission"]``.  The :class:`ToolExecutor`
consults the active :class:`PermissionPolicy` before invoking a tool.

Permission levels (ordered by escalation):

    - ``read``     — pure information retrieval (e.g. ``fs_read``)
    - ``write``    — modifies user state on disk (e.g. ``fs_write``)
    - ``shell``    — invokes external processes (e.g. ``shell_run``)
    - ``network``  — reaches outbound network (e.g. ``web_fetch``)
    - ``danger``   — anything explicitly dangerous (force pushes, deletes)

A permission is granted when it appears in the policy's ``allowed`` set.
By default ``read`` and ``network`` are allowed.  ``write`` / ``shell`` /
``danger`` are denied unless explicitly granted via env or a confirm hook.

Environment overrides:
    ``ALEX_TOOL_PERMISSIONS`` — comma-separated list, e.g. ``"read,write,shell"``
    ``ALEX_TOOL_DENY``         — comma-separated denylist applied last

Beyond access control, the policy mediates two more things:

- **Approval requests** — every gated invocation builds a
  :class:`ToolApprovalRequest`.  Tools may register an
  ``approval_summariser`` (via :func:`attach_approval_summariser`) that
  decorates the request with a one-line ``summary`` and rich
  :class:`PreviewBlock` entries (diff, argv, cwd, …).  The confirm hook
  receives the full request so the UI can render context before asking
  the user to decide.

- **Audit log** — every decision (granted, denied, auto-decided) is
  appended to a JSONL file via :class:`AuditLogger`.  Failures inside
  the logger never propagate; tool execution must not depend on the
  audit path being writable.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from langchain_core.tools import BaseTool

# A confirm hook returns ``True``/``False`` or ``(granted, remember)``.
ConfirmResult = bool | tuple[bool, bool]


# Built-in permission identifiers.  Tool authors should reuse these
# strings rather than introducing ad-hoc names.
PERMISSION_READ = "read"
PERMISSION_WRITE = "write"
PERMISSION_SHELL = "shell"
PERMISSION_NETWORK = "network"
PERMISSION_DANGER = "danger"

KNOWN_PERMISSIONS = frozenset({
    PERMISSION_READ,
    PERMISSION_WRITE,
    PERMISSION_SHELL,
    PERMISSION_NETWORK,
    PERMISSION_DANGER,
})

DEFAULT_ALLOWED = frozenset({PERMISSION_READ, PERMISSION_NETWORK})

DEFAULT_AUDIT_PATH = Path.home() / ".alex" / "audit" / "permissions.jsonl"


# ── approval request shape ────────────────────────────────────────────


@dataclass
class PreviewBlock:
    """A titled preview chunk shown in the permission confirm modal.

    ``kind`` controls how the body is rendered:

    - ``text`` — plain text (default)
    - ``diff`` — unified diff with syntax highlighting
    - ``code`` — fixed-width body, no syntax highlighting
    """

    title: str
    body: str
    kind: str = "text"


@dataclass
class ToolApprovalRequest:
    """Everything the confirm hook needs to render a meaningful prompt."""

    tool_name: str
    permission: str
    args: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    preview: list[PreviewBlock] = field(default_factory=list)


# Summarisers are async because some (notably ``fs_write``) need to read
# the existing file before they can produce a diff.  They may return a
# ``str`` (summary only), a ``(summary, preview_list)`` tuple, or a
# fully-formed :class:`ToolApprovalRequest`.
ApprovalSummariser = Callable[[dict[str, Any]], Awaitable[Any]]
ConfirmHook = Callable[[ToolApprovalRequest], Awaitable[ConfirmResult]]


_SUMMARISER_ATTR = "_alex_summariser"


def attach_approval_summariser(tool: BaseTool, summariser: ApprovalSummariser) -> BaseTool:
    """Bind *summariser* to *tool* so the gating layer can call it."""
    object.__setattr__(tool, _SUMMARISER_ATTR, summariser)
    return tool


def get_approval_summariser(tool: BaseTool) -> ApprovalSummariser | None:
    return getattr(tool, _SUMMARISER_ATTR, None)


# ── audit log ─────────────────────────────────────────────────────────


@dataclass
class AuditEvent:
    """Single decision record persisted by :class:`AuditLogger`."""

    ts: float
    tool_name: str
    permission: str
    decision: str  # allow_once | allow_always | deny | auto_allow | auto_deny
    args_digest: str = ""
    reason: str = ""


class AuditLogger:
    """Append-only JSONL writer for permission decisions.

    Writes are serialised through an :class:`asyncio.Lock` and offloaded
    to a worker thread via :func:`asyncio.to_thread` so the file IO does
    not block the agent's event loop.  Errors (missing parent, full
    disk, permission denied) are swallowed: an absent audit log must
    never block tool execution.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_AUDIT_PATH
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def record(self, event: AuditEvent) -> None:
        try:
            async with self._lock:
                await asyncio.to_thread(self._write, event)
        except Exception:
            return

    def _write(self, event: AuditEvent) -> None:
        payload = {
            "ts": event.ts,
            "iso": datetime.fromtimestamp(event.ts, tz=timezone.utc).isoformat(),
            "tool": event.tool_name,
            "permission": event.permission,
            "decision": event.decision,
            "args_digest": event.args_digest,
            "reason": event.reason,
        }
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)

    def read_all(self) -> list[dict]:
        """Read the audit log into a list of dicts (sync, for tests)."""
        if not self._path.exists():
            return []
        out: list[dict] = []
        with open(self._path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return out


# ── policy ────────────────────────────────────────────────────────────


@dataclass
class PermissionPolicy:
    """Decides whether a tool requiring a given permission may run."""

    allowed: set[str] = field(default_factory=lambda: set(DEFAULT_ALLOWED))
    denied: set[str] = field(default_factory=set)
    confirm_hook: ConfirmHook | None = None
    remember_grants: bool = True
    audit_logger: AuditLogger | None = None

    @classmethod
    def from_env(cls, *, confirm_hook: ConfirmHook | None = None,
                 audit_logger: AuditLogger | None = None) -> "PermissionPolicy":
        """Build a policy from ``ALEX_TOOL_PERMISSIONS`` / ``ALEX_TOOL_DENY``."""
        allowed = set(DEFAULT_ALLOWED)
        denied: set[str] = set()

        raw_allow = os.environ.get("ALEX_TOOL_PERMISSIONS", "").strip()
        if raw_allow:
            allowed = {tok.strip().lower() for tok in raw_allow.split(",") if tok.strip()}

        raw_deny = os.environ.get("ALEX_TOOL_DENY", "").strip()
        if raw_deny:
            denied = {tok.strip().lower() for tok in raw_deny.split(",") if tok.strip()}

        return cls(
            allowed=allowed,
            denied=denied,
            confirm_hook=confirm_hook,
            audit_logger=audit_logger,
        )

    def is_known(self, permission: str) -> bool:
        return permission in KNOWN_PERMISSIONS

    async def check(self, tool_name: str, required_permission: str | None) -> tuple[bool, str]:
        """Backward-compatible thin wrapper around :meth:`check_request`.

        Used when no rich preview information is available — the audit
        record will still be emitted but ``args_digest`` will be blank.
        """
        if not required_permission:
            return True, ""
        request = ToolApprovalRequest(
            tool_name=tool_name,
            permission=required_permission,
        )
        return await self.check_request(request)

    async def check_request(self, request: ToolApprovalRequest) -> tuple[bool, str]:
        """Evaluate *request* and emit an :class:`AuditEvent` for the decision.

        Returns ``(granted, reason)``.  When granted, the empty string
        is returned as the reason.  When denied, *reason* describes why
        (so the gating layer can surface it back to the agent as
        ``Error: tool 'x' blocked: ...``).
        """
        perm = (request.permission or "").strip().lower()
        if not perm:
            return True, ""

        granted, reason, decision = await self._evaluate(request, perm)
        await self._emit_audit(request, perm, decision, reason)
        return granted, reason

    async def _evaluate(
        self, request: ToolApprovalRequest, perm: str,
    ) -> tuple[bool, str, str]:
        if perm in self.denied:
            return False, f"permission '{perm}' explicitly denied", "auto_deny"
        if perm in self.allowed:
            return True, "", "auto_allow"

        if self.confirm_hook is not None:
            result = await self.confirm_hook(request)
            granted, remember = _normalise_confirm_result(
                result, default_remember=self.remember_grants,
            )
            if granted:
                if remember:
                    self.allowed.add(perm)
                return True, "", "allow_always" if remember else "allow_once"
            return False, f"user denied permission '{perm}'", "deny"

        return (
            False,
            f"permission '{perm}' not granted (allowed={sorted(self.allowed) or 'none'})",
            "auto_deny",
        )

    async def _emit_audit(
        self,
        request: ToolApprovalRequest,
        perm: str,
        decision: str,
        reason: str,
    ) -> None:
        if self.audit_logger is None:
            return
        digest = (request.summary or _default_args_digest(request.args))[:240]
        await self.audit_logger.record(AuditEvent(
            ts=time.time(),
            tool_name=request.tool_name,
            permission=perm,
            decision=decision,
            args_digest=digest,
            reason=reason,
        ))


def _normalise_confirm_result(result: ConfirmResult, *, default_remember: bool) -> tuple[bool, bool]:
    if isinstance(result, tuple):
        granted = bool(result[0])
        remember = bool(result[1]) if len(result) > 1 else default_remember
        return granted, remember
    return bool(result), default_remember


def _default_args_digest(args: dict[str, Any]) -> str:
    """One-line ``key=value`` summary used when no summariser is registered."""
    parts: list[str] = []
    for key, value in args.items():
        s = value if isinstance(value, str) else repr(value)
        if len(s) > 80:
            s = s[:77] + "..."
        parts.append(f"{key}={s}")
        if len(parts) >= 4:
            parts.append("…")
            break
    return ", ".join(parts)


def required_permission(tool: BaseTool) -> str | None:
    """Helper for callers — read the required permission off a LangChain tool."""
    metadata = getattr(tool, "metadata", None) or {}
    perm = metadata.get("required_permission")
    if perm is None:
        return None
    return str(perm).strip().lower() or None


# ── permission gating for tools invoked via LangGraph directly ────────


_GATED_MARKER = "_alex_permission_gated"


async def build_approval_request(
    tool: BaseTool, permission: str, args: dict[str, Any],
) -> ToolApprovalRequest:
    """Build a :class:`ToolApprovalRequest` for *tool* with the active *args*.

    Consults the registered summariser if any; otherwise falls back to
    a generic ``key=value`` digest.  Errors inside a summariser are
    surfaced as a degraded summary so the user can still make an
    informed decision.
    """
    summariser = get_approval_summariser(tool)
    if summariser is None:
        return ToolApprovalRequest(
            tool_name=tool.name,
            permission=permission,
            args=dict(args),
            summary=_default_args_digest(args),
        )

    try:
        result = await summariser(args)
    except Exception as e:  # noqa: BLE001 — degrade gracefully
        return ToolApprovalRequest(
            tool_name=tool.name,
            permission=permission,
            args=dict(args),
            summary=f"(summariser failed: {type(e).__name__}: {e})",
        )

    if isinstance(result, ToolApprovalRequest):
        return result
    if isinstance(result, tuple) and len(result) == 2:
        summary, preview = result
        return ToolApprovalRequest(
            tool_name=tool.name,
            permission=permission,
            args=dict(args),
            summary=str(summary),
            preview=list(preview),
        )
    if isinstance(result, str):
        return ToolApprovalRequest(
            tool_name=tool.name,
            permission=permission,
            args=dict(args),
            summary=result,
        )
    return ToolApprovalRequest(
        tool_name=tool.name,
        permission=permission,
        args=dict(args),
        summary=str(result),
    )


def gate_tool_with_policy(tool: BaseTool, policy: PermissionPolicy) -> BaseTool:
    """Wrap *tool*'s underlying coroutine so it consults *policy* before running.

    LangGraph drives tool invocation through ``BaseTool.ainvoke`` — it does
    not go through our :class:`ToolExecutor` — so the policy must be
    enforced at the tool level for user-turn tools.  We wrap the
    ``coroutine`` field on ``StructuredTool`` (the only kind of tool the
    registry actually accepts today) using :func:`object.__setattr__` to
    bypass pydantic's assignment validation.

    This helper is idempotent: repeated calls with the same tool reuse
    the existing wrapper and only swap the bound policy, so callers can
    refresh the policy at runtime (e.g. after attaching a TUI confirm hook).
    """
    perm = required_permission(tool)
    if perm is None:
        return tool

    state = getattr(tool, _GATED_MARKER, None)
    if state is not None:
        state["policy"] = policy
        return tool

    original_coroutine = getattr(tool, "coroutine", None)
    if original_coroutine is None:
        return tool

    state = {"policy": policy}

    async def _gated_coroutine(*args: Any, **kwargs: Any) -> Any:
        active: PermissionPolicy = state["policy"]
        request = await build_approval_request(tool, perm, kwargs)
        granted, reason = await active.check_request(request)
        if not granted:
            return f"Error: tool '{tool.name}' blocked: {reason}"
        return await original_coroutine(*args, **kwargs)

    object.__setattr__(tool, "coroutine", _gated_coroutine)
    object.__setattr__(tool, _GATED_MARKER, state)
    return tool


def gate_tools_with_policy(tools: list[BaseTool], policy: PermissionPolicy) -> list[BaseTool]:
    """Bulk-wrap each tool in *tools* via :func:`gate_tool_with_policy`."""
    for tool in tools:
        gate_tool_with_policy(tool, policy)
    return tools


def is_gated(tool: BaseTool) -> bool:
    """Return ``True`` when *tool* has already been wrapped by gate_tool_with_policy."""
    return getattr(tool, _GATED_MARKER, None) is not None
