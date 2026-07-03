"""Tests for the current Agent facade.

The refactor intentionally moved most orchestration APIs into
``ChatAppService`` and ``AgentModule``.  ``Agent`` is now a thin wrapper
that owns a chat service, tracks session context, and delegates lifecycle
and user-turn execution.
"""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from alex.agent.service import Agent
from alex.bus import AsyncEventBus


def _make_test_agent(**kwargs) -> Agent:
    return Agent(bus=AsyncEventBus(), **kwargs)


class TestAgentSessionContext:
    def test_set_session_context_updates_agent_and_chat_service(self):
        agent = _make_test_agent(llm=MagicMock())
        agent._chat.set_session_context = MagicMock()

        cron_history = [{"job_id": "job-1"}]
        agent.set_session_context("session-1", cron_history)

        assert agent.session_id == "session-1"
        agent._chat.set_session_context.assert_called_once_with("session-1", cron_history)

    def test_bind_event_bus_updates_agent_and_chat_service(self):
        agent = _make_test_agent(llm=MagicMock())
        agent._chat.set_event_bus = MagicMock()
        new_bus = AsyncEventBus()

        agent.bind_event_bus(new_bus)

        assert agent.bus is new_bus
        agent._chat.set_event_bus.assert_called_once_with(new_bus)


class TestAgentLifecycle:
    @pytest.mark.asyncio
    async def test_start_services_initializes_default_llm_when_missing(self):
        agent = _make_test_agent()
        agent._bus.start = AsyncMock()
        agent._chat.set_llm = MagicMock()

        with patch("alex.config.get_llm_config", return_value="cfg") as mock_cfg:
            with patch("alex.llm.client.ChatClient", return_value="client") as mock_client:
                await agent.start_services()

        mock_cfg.assert_called_once_with()
        mock_client.assert_called_once_with("cfg")
        agent._chat.set_llm.assert_called_once_with("client")
        agent._bus.start.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_start_services_reuses_existing_llm(self):
        llm = MagicMock()
        agent = _make_test_agent(llm=llm)
        agent._bus.start = AsyncMock()
        agent._chat.set_llm = MagicMock()

        with patch("alex.config.get_llm_config") as mock_cfg:
            with patch("alex.llm.client.ChatClient") as mock_client:
                await agent.start_services()

        mock_cfg.assert_not_called()
        mock_client.assert_not_called()
        agent._chat.set_llm.assert_not_called()
        agent._bus.start.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_shutdown_delegates_to_chat_service(self):
        agent = _make_test_agent(llm=MagicMock())
        agent._chat.shutdown = AsyncMock()

        await agent.shutdown()

        agent._chat.shutdown.assert_awaited_once_with()


class TestAgentChatDelegation:
    @pytest.mark.asyncio
    async def test_chat_stream_delegates_to_chat_service(self):
        agent = _make_test_agent(llm=MagicMock())
        agent._chat.chat_stream = AsyncMock()

        await agent.chat_stream("hello")

        agent._chat.chat_stream.assert_awaited_once_with("hello", session_id="")

    def test_last_turn_result_proxies_chat_service(self):
        agent = _make_test_agent(llm=MagicMock())
        result = {"status": "ok"}
        with patch.object(type(agent._chat), "last_turn_result", new_callable=PropertyMock) as mocked:
            mocked.return_value = result
            assert agent.last_turn_result is result
