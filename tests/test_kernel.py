"""Unit tests for alex.kernel — DTOs, contracts, and protocols.

Phase 1: ensure the shared kernel is well-formed, DTOs round-trip correctly,
and contracts have the expected hierarchy.
"""

from alex.kernel.bus import (
    Command,
    Event,
    Request,
    correlation_id,
)
from alex.kernel.dto.message import MessageDTO
from alex.kernel.dto.skill import SkillCard
from alex.kernel.dto.tool import ToolResult, ToolSpec
from alex.kernel.errors import CapabilityTimeout, CapabilityUnavailable, HandlerError


# ── MessageDTO ────────────────────────────────────────────────────────────────


class TestMessageDTO:
    def test_basic_round_trip(self):
        """MessageDTO → to_dict → from_dict preserves all fields."""
        original = MessageDTO(
            role="assistant",
            content="Hello, world!",
            reasoning_content="The user is greeting me.",
        )
        d = original.to_dict()
        restored = MessageDTO.from_dict(d)

        assert restored.role == "assistant"
        assert restored.content == "Hello, world!"
        assert restored.reasoning_content == "The user is greeting me."

    def test_reasoning_content_preserved(self):
        """Constraint 1: reasoning_content must survive round-trip losslessly."""
        original = MessageDTO(
            role="assistant",
            content="Answer",
            reasoning_content="Deep thinking here\n多行\n思考",
        )
        d = original.to_dict()
        restored = MessageDTO.from_dict(d)

        assert restored.reasoning_content == "Deep thinking here\n多行\n思考"

    def test_tool_calls_round_trip(self):
        """Tool calls should round-trip correctly."""
        tool_calls = [
            {
                "id": "call_123",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "/tmp/x"}'},
            }
        ]
        original = MessageDTO(role="assistant", content="", tool_calls=tool_calls)
        d = original.to_dict()
        restored = MessageDTO.from_dict(d)

        assert restored.tool_calls == tool_calls

    def test_tool_message_round_trip(self):
        """Tool result messages should round-trip."""
        original = MessageDTO(
            role="tool",
            content="file contents here",
            tool_call_id="call_123",
            name="read_file",
        )
        d = original.to_dict()
        restored = MessageDTO.from_dict(d)

        assert restored.role == "tool"
        assert restored.tool_call_id == "call_123"
        assert restored.name == "read_file"

    def test_empty_reasoning_content_defaults_to_empty_string(self):
        """When reasoning_content is missing, it should default to ''."""
        d = {"role": "assistant", "content": "hi"}
        msg = MessageDTO.from_dict(d)
        assert msg.reasoning_content == ""

    def test_metadata_field(self):
        """Metadata should be preserved."""
        original = MessageDTO(role="user", content="hi", metadata={"source": "cron"})
        d = original.to_dict()
        # metadata is NOT serialized to the dict (it's cross-module metadata)
        restored = MessageDTO.from_dict(d)
        # metadata is preserved on the DTO object itself
        assert original.metadata == {"source": "cron"}


# ── ToolSpec / ToolResult ─────────────────────────────────────────────────────


class TestToolSpec:
    def test_basic_spec(self):
        spec = ToolSpec(
            name="read_file",
            description="Read a file from disk",
            json_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            provider="builtin",
        )
        assert spec.name == "read_file"
        assert spec.provider == "builtin"

    def test_to_openai_schema(self):
        spec = ToolSpec(
            name="my_tool",
            description="Does things",
            json_schema={"type": "object", "properties": {}},
        )
        schema = spec.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "my_tool"
        assert schema["function"]["description"] == "Does things"

    def test_default_provider_is_builtin(self):
        spec = ToolSpec(name="t", description="d")
        assert spec.provider == "builtin"


class TestToolResult:
    def test_success_result(self):
        r = ToolResult(name="read_file", output="file contents", run_id="run_1")
        assert r.ok is True
        assert r.is_error is False

    def test_error_result(self):
        r = ToolResult(name="read_file", error="File not found", run_id="run_2")
        assert r.ok is False
        assert r.is_error is True


# ── SkillCard ─────────────────────────────────────────────────────────────────


class TestSkillCard:
    def test_defaults(self):
        card = SkillCard()
        assert card.name == ""
        assert card.status == "ACTIVE"
        assert card.tags == []

    def test_full_card(self):
        card = SkillCard(
            id="sk_001",
            name="Code Review",
            pattern="review|audit",
            summary="Review code changes",
            instruction="Step 1: Read the diff...",
            tags=["code", "review"],
            version=3,
        )
        assert card.id == "sk_001"
        assert card.version == 3


# ── Error types ───────────────────────────────────────────────────────────────


class TestErrors:
    def test_capability_unavailable(self):
        err = CapabilityUnavailable("GetToolCatalog")
        assert "GetToolCatalog" in str(err)
        assert err.request_type == "GetToolCatalog"

    def test_capability_timeout(self):
        err = CapabilityTimeout("ExecuteTool", 5.0, correlation_id="abc123")
        assert "ExecuteTool" in str(err)
        assert "5.0" in str(err)
        assert err.timeout == 5.0
        assert err.correlation_id == "abc123"

    def test_handler_error_chains_original(self):
        original = ValueError("bad input")
        # When the bus raises HandlerError, it uses 'raise ... from exc'
        # which sets __cause__.  Verify the chain by actually raising.
        try:
            try:
                raise original
            except ValueError as exc:
                raise HandlerError("ExecuteTool", exc) from exc
        except HandlerError as err:
            assert err.original is original
            assert "ValueError" in str(err)
            assert err.__cause__ is original


# ── Event / Command / Request hierarchy ───────────────────────────────────────


class TestMessageHierarchy:
    def test_event_has_default_fields(self):
        evt = Event()
        assert evt.event_id != ""
        assert evt.session_id == ""
        assert evt.trace_id == ""

    def test_command_is_event(self):
        cmd = Command(session_id="s1")
        assert isinstance(cmd, Event)
        assert cmd.session_id == "s1"

    def test_request_is_not_event(self):
        """Request has its own base class — it's NOT an Event (different dispatch)."""
        req = Request()
        assert not isinstance(req, Event)

    def test_correlation_id_is_unique(self):
        ids = {correlation_id() for _ in range(100)}
        assert len(ids) == 100


# ── Contract imports ──────────────────────────────────────────────────────────


class TestContractImports:
    """Verify that all contract modules are importable and have the right bases."""

    def test_chat_contracts_are_events_or_commands(self):
        from alex.kernel.contracts.chat import (
            ThinkingUpdated,
            TokenEmitted,
            TurnCompleted,
            TurnFailed,
            TurnStarted,
            UserTurnRequested,
        )
        assert issubclass(TokenEmitted, Event)
        assert issubclass(ThinkingUpdated, Event)
        assert issubclass(TurnStarted, Event)
        assert issubclass(TurnCompleted, Event)
        assert issubclass(TurnFailed, Event)
        assert issubclass(UserTurnRequested, Command)

    def test_tools_contracts(self):
        from alex.kernel.contracts.tools import (
            ExecuteTool,
            GetToolCatalog,
            ToolApprovalRequested,
            ToolApprovalResolved,
            ToolsProvided,
        )
        assert issubclass(ExecuteTool, Request)
        assert issubclass(GetToolCatalog, Request)
        assert issubclass(ToolApprovalRequested, Event)
        assert issubclass(ToolApprovalResolved, Event)
        assert issubclass(ToolsProvided, Event)

    def test_skills_contracts(self):
        from alex.kernel.contracts.skills import (
            LoadSkill,
            ReflectSkills,
            RetrieveSkills,
            SkillsReflected,
        )
        assert issubclass(RetrieveSkills, Request)
        assert issubclass(LoadSkill, Request)
        assert issubclass(ReflectSkills, Command)
        assert issubclass(SkillsReflected, Event)

    def test_memory_contracts_are_all_requests(self):
        from alex.kernel.contracts.memory import (
            AppendMessages,
            ClearMemory,
            GetContext,
            ReplaceMemory,
        )
        assert issubclass(GetContext, Request)
        assert issubclass(AppendMessages, Request)
        assert issubclass(ReplaceMemory, Request)
        assert issubclass(ClearMemory, Request)

    def test_session_contracts(self):
        from alex.kernel.contracts.session import (
            ListSessions,
            LoadSession,
        )
        assert issubclass(ListSessions, Request)
        assert issubclass(LoadSession, Request)

    def test_cron_contracts(self):
        from alex.kernel.contracts.cron import (
            CancelCron,
            CronJobEvent,
            CronTurnRequested,
            ScheduleCron,
        )
        assert issubclass(ScheduleCron, Request)
        assert issubclass(CancelCron, Request)
        assert issubclass(CronTurnRequested, Command)
        assert issubclass(CronJobEvent, Event)
