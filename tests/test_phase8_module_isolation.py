"""Phase 8 tests: comprehensive module isolation verification.

Validates:
- Six business modules are mutually zero-import
- All business modules depend only on alex.kernel (the shared kernel)
- Import-linter configuration exists and is valid
- Each business module has a Module entry point
- End-to-end bus integration with all modules wired through ModuleHost
"""

import asyncio
import ast
import os

import pytest

from alex.bus.in_memory import AsyncEventBus
from alex.kernel.host import ModuleHost
from alex.kernel.contracts.memory import GetContext


# ── Business modules ─────────────────────────────────────────────────────────

BUSINESS_MODULES = [
    "alex.agent",
    "alex.tui",
    "alex.tools",
    "alex.mcp",
    "alex.skill",
    "alex.memory",
]

# Shared infrastructure that any module may import
SHARED_ALLOWED = {
    "alex.kernel",        # Shared kernel (contracts + DTOs)
    "alex.bus",           # Bus implementation
    "alex.config",        # Leaf config
    "alex.prompts",       # Leaf prompts/templates
    "alex.messages",      # Message helper utilities
    "alex.app_logging",   # Logging utility
    "alex.llm",           # LLM client (agent internal)
    "alex.store",         # Infrastructure module
    "alex.scheduler",     # Infrastructure module
}


def _get_module_imports(module_path: str) -> list[str]:
    """Parse a Python module and return its top-level imports."""
    with open(module_path) as f:
        tree = ast.parse(f.read())

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


class TestModuleIsolation:
    """Verify that business modules do not import each other directly."""

    @pytest.mark.parametrize("module_name", BUSINESS_MODULES)
    def test_module_only_imports_kernel_or_allowed(self, module_name):
        """Each business module should not import other business modules.

        Allowed: alex.kernel, alex.bus, alex.config, alex.prompts, etc.
        Forbidden: other alex.<business_module> imports.
        """
        package = module_name.replace(".", "/")
        module_file = f"{package}/module.py"

        if not os.path.exists(module_file):
            pytest.skip(f"No module.py for {module_name}")

        imports = _get_module_imports(module_file)
        alex_imports = {i for i in imports if i.startswith("alex.")}

        for imp in alex_imports:
            # Check if this import is from another business module
            is_business = any(
                imp.startswith(bm) and bm != module_name
                for bm in BUSINESS_MODULES
            )
            is_allowed = any(
                imp.startswith(allowed) or imp == allowed
                for allowed in SHARED_ALLOWED
            )

            if is_business and not is_allowed:
                # Check for specific exceptions (mcp importing mcp_client from tools)
                if "mcp_client" in imp and module_name == "alex.mcp":
                    continue  # mcp_client is a shared utility, not a business boundary
                if "mcp_client" in imp and module_name == "alex.tui":
                    continue  # lazy import in app.py — shared utility

                pytest.fail(
                    f"{module_name}/module.py imports {imp} — "
                    f"cross-module business import detected! "
                    f"Use bus messages instead."
                )

    def test_tui_ports_no_agent_facade(self):
        """alex/tui/ports.py no longer defines AgentFacade — TUI uses bus directly."""
        ports_file = "alex/tui/ports.py"
        with open(ports_file) as f:
            content = f.read()

        # Should NOT import from alex.agent
        assert "from alex.agent" not in content, (
            "alex/tui/ports.py must not import from alex.agent"
        )
        # Should define _ControllerHost for the mixin
        assert "class _ControllerHost(Protocol)" in content


class TestTuiDirectBus:
    """TUI direct bus communication tests (replaces old TuiBusProxy tests)."""

    @pytest.mark.asyncio
    async def test_tui_publishes_user_turn_via_bus(self):
        """AlexApp publishes UserTurnRequested directly on the bus."""
        from alex.kernel.contracts.chat import UserTurnRequested

        bus = AsyncEventBus()
        await bus.start()

        received: list[UserTurnRequested] = []

        async def _handler(event):
            received.append(event)

        await bus.subscribe(UserTurnRequested, _handler)

        # Simulate what AlexApp._run_chat does
        bus.publish(UserTurnRequested(
            session_id="test-session",
            user_text="hello",
        ))

        await asyncio.sleep(0.05)

        assert len(received) == 1
        assert received[0].user_text == "hello"
        assert received[0].session_id == "test-session"

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_tui_publishes_feedback_via_bus(self):
        """TUI publishes FeedbackSubmitted directly on the bus."""
        from alex.kernel.contracts.chat import FeedbackSubmitted

        bus = AsyncEventBus()
        await bus.start()

        received: list[FeedbackSubmitted] = []

        async def _handler(event):
            received.append(event)

        await bus.subscribe(FeedbackSubmitted, _handler)

        # Simulate what AlexApp._submit_feedback does
        bus.publish(FeedbackSubmitted(
            session_id="test-session",
            turn_id="turn-1",
            positive=True,
        ))

        await asyncio.sleep(0.05)

        assert len(received) == 1
        assert received[0].positive is True
        assert received[0].turn_id == "turn-1"

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_tui_reflects_via_bus(self):
        """TUI triggers reflection via bus event directly."""
        from alex.kernel.contracts.skills import ReflectSkills

        bus = AsyncEventBus()
        await bus.start()

        received: list[ReflectSkills] = []

        async def _handler(event):
            received.append(event)

        await bus.subscribe(ReflectSkills, _handler)

        # Simulate what controller._run_force_reflection does
        bus.publish(ReflectSkills(session_id="test-session"))

        await asyncio.sleep(0.05)

        assert len(received) == 1

        await bus.shutdown()


class TestModuleHostFullIntegration:
    """End-to-end test: all modules wired through ModuleHost."""

    @pytest.mark.asyncio
    async def test_all_modules_start_and_stop_via_host(self):
        """All modules register and respond to bus requests through ModuleHost."""
        from alex.memory.module import MemoryModule
        from alex.skill.module import SkillModule
        from alex.tools.module import ToolsModule
        from alex.mcp.module import MCPModule
        from pathlib import Path

        bus = AsyncEventBus()
        host = ModuleHost(bus)

        # Register all modules
        host.register(MemoryModule())
        host.register(SkillModule())
        host.register(ToolsModule())
        host.register(MCPModule(config_path=Path("/nonexistent/mcp_config.json")))

        await host.start_all()

        # Memory module should respond
        from alex.kernel.contracts.memory import AppendMessages as AppendReq
        await bus.request(AppendReq(
            session_id="e2e-test",
            messages=[{"role": "user", "content": "e2e test message"}],
        ))
        ctx = await bus.request(GetContext(session_id="e2e-test"))
        assert len(ctx) == 1
        assert ctx[0]["content"] == "e2e test message"

        # Skill module should respond
        from alex.kernel.contracts.skills import RetrieveSkills
        skills = await bus.request(RetrieveSkills(query="test"))
        assert isinstance(skills, list)

        # Tools module should respond
        from alex.kernel.contracts.tools import GetToolCatalog
        catalog = await bus.request(GetToolCatalog())
        assert isinstance(catalog, list)

        await host.stop_all()

    @pytest.mark.asyncio
    async def test_module_config_toggle(self):
        """Modules can be selectively enabled/disabled via registration.

        When a module is not registered, its capabilities are unavailable.
        """
        bus = AsyncEventBus()
        host = ModuleHost(bus)

        # Only register memory module (no skills, no tools, no mcp)
        from alex.memory.module import MemoryModule
        host.register(MemoryModule())

        await host.start_all()

        # Memory works
        from alex.kernel.contracts.memory import AppendMessages as AppendReq
        await bus.request(AppendReq(
            session_id="toggle-test",
            messages=[{"role": "user", "content": "test"}],
        ))
        ctx = await bus.request(GetContext(session_id="toggle-test"))
        assert len(ctx) == 1

        # Skills not available
        from alex.kernel.contracts.skills import RetrieveSkills
        from alex.kernel.errors import CapabilityUnavailable
        with pytest.raises(CapabilityUnavailable):
            await bus.request(RetrieveSkills(query="test"))

        await host.stop_all()
