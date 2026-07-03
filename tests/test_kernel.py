"""Unit tests for alex.kernel — DTOs, contracts, and protocols.

Phase 1: ensure the shared kernel is well-formed, DTOs round-trip correctly,
and contracts have the expected hierarchy.
"""

from alex.kernel.dto.message import MessageDTO
from alex.kernel.dto.skill import SkillCard
from alex.kernel.dto.tool import ToolSpec
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



# ── SkillCard ─────────────────────────────────────────────────────────────────


class TestSkillCard:
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



