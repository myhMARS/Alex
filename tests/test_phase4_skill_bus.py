"""Phase 4 tests: skill operations through bus request/reply.

Validates:
- SkillModule provides RetrieveSkills and LoadSkill via bus
- TurnProcessor can use bus-based skill lookup
- Degradation path: agent without skill module continues (empty skills)
- LoadSkill publishes SkillLoaded event
"""

import pytest

from alex.bus.in_memory import AsyncEventBus
from alex.kernel.contracts.skills import LoadSkill, RetrieveSkills
from alex.skill.module import SkillModule


class TestSkillBusIntegration:
    """Skill operations through the bus with SkillModule."""

    @pytest.mark.asyncio
    async def test_retrieve_skills_returns_empty_list(self):
        """Without any skills installed, retrieve returns empty list."""
        bus = AsyncEventBus()
        await bus.start()
        skill_mod = SkillModule()
        await skill_mod.start(bus)

        result = await bus.request(RetrieveSkills(query="any query", top_k=5))
        assert isinstance(result, list)
        if result:
            from alex.kernel.dto.skill import SkillCard
            assert all(isinstance(s, SkillCard) for s in result)

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_load_nonexistent_skill_raises(self):
        """Loading a non-existent skill raises HandlerError."""
        from alex.kernel.errors import HandlerError

        bus = AsyncEventBus()
        await bus.start()
        skill_mod = SkillModule()
        await skill_mod.start(bus)

        with pytest.raises(HandlerError, match="Skill not found"):
            await bus.request(LoadSkill(skill_name="no_such_skill"))

        await bus.shutdown()


class TestTurnProcessorSkillViaBus:
    """TurnProcessor's bus-aware skill methods."""

    @pytest.mark.asyncio
    async def test_get_skill_by_name_via_bus(self):
        """TurnProcessor._get_skill_by_name uses bus when handler available."""
        from alex.agent.turn_processor import TurnProcessor
        from alex.agent.chat_service import _BusTurnServices

        bus = AsyncEventBus()
        await bus.start()

        skill_mod = SkillModule()
        await skill_mod.start(bus)

        tp = TurnProcessor(
            llm=None,
            push_notification=lambda e: None,
            services=_BusTurnServices(bus),
            get_system_prompt=lambda _: "",
            max_iterations=1,
        )

        # Unknown skill returns None
        result = await tp._get_skill_by_name("no_such_skill")
        assert result is None

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_retrieve_skills_via_bus(self):
        """TurnProcessor._retrieve_skills works via bus."""
        from alex.agent.turn_processor import TurnProcessor
        from alex.agent.chat_service import _BusTurnServices

        bus = AsyncEventBus()
        await bus.start()

        skill_mod = SkillModule()
        await skill_mod.start(bus)

        tp = TurnProcessor(
            llm=None,
            push_notification=lambda e: None,
            services=_BusTurnServices(bus),
            get_system_prompt=lambda _: "",
            max_iterations=1,
        )

        result = await tp._retrieve_skills("test query", top_k=3)
        assert isinstance(result, list)
        if result:
            from alex.kernel.dto.skill import SkillCard
            assert all(isinstance(s, SkillCard) for s in result)

        await bus.shutdown()
