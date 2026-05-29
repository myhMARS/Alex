"""Shared binary-content detection helpers for tool implementations."""

from __future__ import annotations


def looks_like_binary(probe: bytes) -> bool:
    """Best-effort binary detection with a UTF-8 friendly fallback."""
    if b"\x00" in probe:
        return True
    if not probe:
        return False
    try:
        probe.decode("utf-8")
        return False
    except UnicodeDecodeError as e:
        if e.end >= len(probe) - 3:
            try:
                probe[:e.start].decode("utf-8")
                return False
            except UnicodeDecodeError:
                pass
    text_chars = bytes(range(0x20, 0x7F)) + b"\n\r\t\b\f"
    nontext = sum(1 for b in probe if b not in text_chars)
    return nontext / len(probe) > 0.3
