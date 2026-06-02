"""Memory factory — default memory backend for backward compatibility.

Prefer using ``MemoryModule`` via ``ModuleHost`` for new code.
"""

from __future__ import annotations

from alex.memory.base import MemoryBase
from alex.memory.buffer import BufferMemory


def create_default_memory() -> MemoryBase:
    """Build the default session-scoped memory backend."""
    return BufferMemory()
