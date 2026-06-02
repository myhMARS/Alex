"""Module / ModuleHost protocols — the pluggable architecture backbone.

Every business module exposes a single ``Module`` entry point.  On
``start(bus)`` it subscribes to events and provides request handlers —
the module knows nothing about other modules, only the bus.

``ModuleHost`` is the composition root: it discovers modules via
entry-points, calls ``start(bus)`` on each (in dependency order),
and manages the lifecycle.
"""

from __future__ import annotations

from typing import Any, Protocol


class Module(Protocol):
    """Pluggable business module — the single entry point for each domain.

    Each module:
    1. Declares dependencies via ``dependencies`` (list of module names)
    2. Receives the bus on ``start()``
    3. Subscribes to events it cares about
    4. Provides request handlers for capabilities it owns
    5. Cleans up on ``stop()``
    """

    name: str
    dependencies: list[str]

    async def start(self, bus: Any) -> None:
        """Register subscriptions and request handlers on *bus*."""
        ...

    async def stop(self) -> None:
        """Release resources (connections, tasks, etc.)."""
        ...


class ModuleHost(Protocol):
    """Composition root — discovers, wires, and manages modules.

    Starts modules in topological order based on declared dependencies.
    """

    def register(self, module: Module) -> None: ...

    async def start_all(self) -> None: ...

    async def stop_all(self) -> None: ...
