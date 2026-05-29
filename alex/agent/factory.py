"""Agent factory — explicit wiring function for creating a fully-assembled Agent.

Extracts the wiring logic from Agent.__init__ so callers get a ready-to-use
Agent without knowing about internal service construction order.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool as LCBaseTool

from alex.agent.composition import (
    create_default_llm,
    create_default_memory,
    create_default_skill_service,
)
from alex.agent.service import Agent
from alex.bus import AsyncEventBus
from alex.memory.base import MemoryBase
from alex.skill import SkillService
from alex.tools.permissions import AuditLogger, PermissionPolicy
from alex.tools.plugin_loader import PluginLoadResult, install_plugins

logger = logging.getLogger(__name__)


def create_agent(
    *,
    system_prompt: str | None = None,
    max_iterations: int = 5,
    tools: list[LCBaseTool] | None = None,
    callbacks: list[BaseCallbackHandler] | None = None,
    memory: MemoryBase | None = None,
    skill_manager: SkillService | None = None,
    llm: BaseChatModel | None = None,
    event_bus: AsyncEventBus | None = None,
    llm_factory: Callable[[], BaseChatModel] | None = None,
    permissions: PermissionPolicy | None = None,
    audit_logger: AuditLogger | None = None,
    audit_path: Path | None = None,
    plugin_root: Path | None = None,
    enable_plugins: bool = True,
) -> tuple[Agent, list[PluginLoadResult]]:
    """Create a fully-wired Agent with all services composed.

    All parameters are optional; sensible defaults are provided for
    memory, skills, the LLM (via LLMFactory + config), and the
    permission policy (loaded from environment).

    A persistent :class:`AuditLogger` is attached to the policy unless
    one is explicitly passed (or *audit_path* is set to ``False``-y to
    disable audit recording entirely).

    User plugins under ``~/.alex/plugins/*.py`` are loaded by default.
    Pass ``enable_plugins=False`` to skip plugin discovery, or supply
    *plugin_root* to override the default location.

    Returns the agent together with the per-plugin load results so the
    host can surface diagnostics for failures.
    """
    _llm = llm or (llm_factory() if llm_factory else create_default_llm())
    _memory = memory or create_default_memory()
    _skills = skill_manager or create_default_skill_service()
    _system_prompt = system_prompt or "You are a helpful AI assistant."

    if audit_logger is None and audit_path is not False:
        audit_logger = AuditLogger(audit_path) if audit_path else AuditLogger()

    if permissions is None:
        _permissions = PermissionPolicy.from_env(audit_logger=audit_logger)
    else:
        _permissions = permissions
        if _permissions.audit_logger is None and audit_logger is not None:
            _permissions.audit_logger = audit_logger

    agent = Agent(
        system_prompt=_system_prompt,
        max_iterations=max_iterations,
        tools=tools,
        callbacks=callbacks,
        memory=_memory,
        skill_manager=_skills,
        llm=_llm,
        event_bus=event_bus,
        permissions=_permissions,
    )

    plugin_results: list[PluginLoadResult] = []
    if enable_plugins:
        plugin_results = install_plugins(agent, root=plugin_root)
        for result in plugin_results:
            if result.error:
                logger.warning("plugin %s: %s", result.path.name, result.error)
            elif result.tools:
                logger.info("plugin %s registered %d tool(s)", result.path.name, len(result.tools))

    return agent, plugin_results
