"""Plugin loader — discover user-supplied tools from ``~/.alex/plugins``.

A plugin is a Python file that exposes one of the following:

- ``ALEX_TOOLS`` — module-level list of ``AlexTool`` instances
- ``tools()`` — callable returning a list of ``AlexTool`` instances
- ``register(agent)`` — callable that receives the :class:`Agent` and is
  free to call ``agent.register_tool(...)`` itself

Plugins run in-process and are trusted: they have full access to the
host's permissions.  We isolate failures so a single broken plugin does
not prevent other plugins from loading.

This module never imports ``alex.agent.service.Agent`` at module scope
to avoid cycles; the *register* entrypoint receives the agent instance
through the public facade.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alex.tools.models import AlexTool

logger = logging.getLogger(__name__)


DEFAULT_PLUGIN_ROOT = Path.home() / ".alex" / "plugins"


@dataclass
class PluginLoadResult:
    """Outcome of loading a single plugin file."""

    path: Path
    module_name: str
    tools: list[AlexTool]
    registered_via: str  # "ALEX_TOOLS" | "tools()" | "register()" | "none"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _module_name_for(path: Path) -> str:
    return f"alex_plugin_{path.stem}_{abs(hash(str(path.resolve())))}"


def _import_plugin(path: Path) -> Any:
    """Import a plugin file as an isolated module.

    Uses ``importlib.util.spec_from_file_location`` so the plugin does
    not need to live on ``sys.path``.
    """
    module_name = _module_name_for(path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _coerce_tools(value: Any) -> list[AlexTool]:
    """Validate that *value* is an iterable of ``AlexTool`` instances."""
    if value is None:
        return []
    if isinstance(value, AlexTool):
        return [value]
    if not isinstance(value, Iterable):
        raise TypeError(f"expected Iterable[AlexTool], got {type(value).__name__}")
    out: list[AlexTool] = []
    for item in value:
        if not isinstance(item, AlexTool):
            raise TypeError(f"plugin yielded non-AlexTool: {type(item).__name__}")
        out.append(item)
    return out


def discover_plugin_files(root: Path | None = None) -> list[Path]:
    """Return ``.py`` files inside *root* (non-recursive).

    Files starting with ``_`` are skipped so users can keep helper
    modules alongside plugins (``_helpers.py``).
    """
    base = root or DEFAULT_PLUGIN_ROOT
    if not base.exists() or not base.is_dir():
        return []
    return sorted(p for p in base.glob("*.py") if not p.name.startswith("_"))


def load_plugins(
    *,
    root: Path | None = None,
    agent: Any = None,
) -> list[PluginLoadResult]:
    """Discover and load every plugin file under *root*.

    When *agent* is provided, plugins exposing a ``register(agent)``
    entrypoint are invoked with the agent.  Tools returned via
    ``ALEX_TOOLS`` or ``tools()`` are collected on each result and the
    caller is expected to register them via ``agent.register_tool``.
    """
    results: list[PluginLoadResult] = []
    for path in discover_plugin_files(root):
        result = _load_one(path, agent=agent)
        results.append(result)
        if result.error:
            logger.warning("plugin %s failed: %s", path.name, result.error)
    return results


def _load_one(path: Path, *, agent: Any) -> PluginLoadResult:
    module_name = _module_name_for(path)
    try:
        module = _import_plugin(path)
    except Exception as e:
        return PluginLoadResult(
            path=path, module_name=module_name, tools=[], registered_via="none",
            error=f"{type(e).__name__}: {e}",
        )

    # Resolution priority: ALEX_TOOLS > tools() > register(agent)
    if hasattr(module, "ALEX_TOOLS"):
        try:
            tools = _coerce_tools(getattr(module, "ALEX_TOOLS"))
        except TypeError as e:
            return PluginLoadResult(
                path=path, module_name=module_name, tools=[], registered_via="ALEX_TOOLS",
                error=str(e),
            )
        return PluginLoadResult(
            path=path, module_name=module_name, tools=tools, registered_via="ALEX_TOOLS",
        )

    if callable(getattr(module, "tools", None)):
        try:
            tools = _coerce_tools(module.tools())
        except Exception as e:
            return PluginLoadResult(
                path=path, module_name=module_name, tools=[], registered_via="tools()",
                error=f"{type(e).__name__}: {e}",
            )
        return PluginLoadResult(
            path=path, module_name=module_name, tools=tools, registered_via="tools()",
        )

    if callable(getattr(module, "register", None)):
        if agent is None:
            return PluginLoadResult(
                path=path, module_name=module_name, tools=[], registered_via="register()",
                error="plugin defines register(agent) but no agent was provided",
            )
        try:
            module.register(agent)
        except Exception as e:
            return PluginLoadResult(
                path=path, module_name=module_name, tools=[], registered_via="register()",
                error=f"{type(e).__name__}: {e}",
            )
        return PluginLoadResult(
            path=path, module_name=module_name, tools=[], registered_via="register()",
        )

    return PluginLoadResult(
        path=path, module_name=module_name, tools=[], registered_via="none",
        error="plugin does not expose ALEX_TOOLS, tools(), or register(agent)",
    )


def install_plugins(agent: Any, *, root: Path | None = None) -> list[PluginLoadResult]:
    """High-level entry point — load plugins and register the resulting tools.

    Returns the per-plugin results so the host can surface diagnostics.
    """
    results = load_plugins(root=root, agent=agent)
    for result in results:
        if not result.ok or not result.tools:
            continue
        for tool in result.tools:
            try:
                agent.register_tool(tool)
            except Exception as e:  # pragma: no cover - defensive
                result.error = f"register_tool failed: {type(e).__name__}: {e}"
                break
    return results
