"""Concrete ModuleHost — discovers modules via entry-points and manages lifecycle.

Starts modules in topological order based on declared dependencies.
"""

from __future__ import annotations

import logging
from typing import Any

from alex.kernel.runtime import Module

logger = logging.getLogger(__name__)


class ModuleHost:
    """Composition root — wires modules together via a shared MessageBus.

    Modules declare ``dependencies: list[str]`` — names of modules they
    require to be started first. ``start_all()`` performs a topological
    sort and starts modules in dependency order.
    """

    def __init__(self, bus: Any) -> None:
        self._bus = bus
        self._modules: dict[str, Module] = {}
        self._started: list[Module] = []

    def register(self, module: Module) -> None:
        """Add a module to the host. Must be called before ``start_all()``."""
        self._modules[module.name] = module
        logger.info("Registered module: %s", module.name)

    async def start_all(self) -> None:
        """Start the bus, then start each module in dependency order.

        如果中途某模块启动失败，已启动的模块会按逆序停止以保证一致性。
        """
        await self._bus.start()
        order = self._topological_sort()
        try:
            for module in order:
                logger.info("Starting module: %s", module.name)
                await module.start(self._bus)
                self._started.append(module)
            logger.info("All %d modules started.", len(self._started))
        except Exception:
            logger.exception("Module start failed, rolling back already-started modules")
            await self.stop_all()
            raise

    async def stop_all(self) -> None:
        """Stop each module (reverse start order), then shut down the bus."""
        for module in reversed(self._started):
            logger.info("Stopping module: %s", module.name)
            try:
                await module.stop()
            except Exception:
                logger.warning("Error stopping module %s", module.name, exc_info=True)
        self._started.clear()
        await self._bus.shutdown()
        logger.info("All modules stopped.")

    def _topological_sort(self) -> list[Module]:
        """Sort modules so dependencies start first.

        Modules with missing dependencies are still started (with a warning),
        allowing optional modules to be absent.
        """
        visited: set[str] = set()
        order: list[Module] = []

        def visit(name: str) -> None:
            if name in visited:
                return
            visited.add(name)
            module = self._modules.get(name)
            if module is None:
                return  # optional dependency not registered
            for dep in getattr(module, "dependencies", []):
                if dep not in visited:
                    if dep in self._modules:
                        visit(dep)
                    else:
                        logger.debug("Module '%s' depends on '%s' (not registered, skipping)", name, dep)
            order.append(module)

        for name in self._modules:
            visit(name)
        return order

    @property
    def bus(self) -> Any:
        return self._bus

    @property
    def modules(self) -> list[Module]:
        return list(self._started)
