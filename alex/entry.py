"""Production entry point — wires modules via ModuleHost with dependency ordering."""

from __future__ import annotations

import asyncio
import logging
from importlib.metadata import entry_points

from alex.app_logging import configure_logging
from alex.bus import AsyncEventBus
from alex.kernel.host import ModuleHost

logger = logging.getLogger(__name__)


def main() -> None:
    log_path = configure_logging()
    logger.info("Alex logging initialized at %s", log_path)

    async def _run() -> None:
        bus = AsyncEventBus()
        host = ModuleHost(bus)

        for ep in entry_points(group="alex.modules"):
            if ep.name == "tui":
                continue  # TUI runs after all modules start
            logger.info("Loading module: %s", ep.name)
            host.register(ep.load()())

        await host.start_all()
        logger.info("All modules started via ModuleHost")

        from alex.tui import AlexApp
        app = AlexApp(bus, host_managed=True)

        try:
            await app.run_async()
        finally:
            await host.stop_all()

    asyncio.run(_run())
