"""Shared helpers for presenting tool-call output in the TUI."""

from __future__ import annotations

from pathlib import Path


def is_read_tool_name(name: str) -> bool:
    return str(name or "").strip().lower() == "read"


def extract_read_path(tool_call: dict) -> str:
    output = str(tool_call.get("output") or "")
    if output.startswith("Path: "):
        first = output.splitlines()[0].strip()
        if first.startswith("Path: "):
            return first[len("Path: "):].strip()
    args = tool_call.get("args")
    if isinstance(args, dict):
        path = args.get("path")
        if path:
            return str(path)
    return "(unknown path)"


def format_read_display_path(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return "(unknown path)"
    cwd = str(Path.cwd()).replace("\\", "/").rstrip("/")
    lowered = text.lower()
    lowered_cwd = cwd.lower()
    if cwd and (lowered == lowered_cwd or lowered.startswith(lowered_cwd + "/")):
        relative = text[len(cwd):].lstrip("/")
        return relative or "."
    if ":/" in text or text.startswith("/"):
        parts = [part for part in text.split("/") if part]
        if parts and parts[0].endswith(":"):
            parts = parts[1:]
        if len(parts) >= 3:
            return "/".join(parts[-3:])
        if parts:
            return "/".join(parts)
    return text


def read_output_paths(output: str) -> list[str]:
    return [line.strip() for line in str(output).splitlines() if line.strip()]
