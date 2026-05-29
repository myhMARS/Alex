"""Shared path safety helpers for tool implementations."""

from __future__ import annotations

from pathlib import Path


def resolve_path_in_allowed_roots(
    raw: str | None,
    allowed_roots: list[Path],
    *,
    default_to_first_root: bool = False,
    default_to_cwd: bool = False,
    label: str = "path",
) -> Path:
    """Resolve *raw* and ensure it stays under one of *allowed_roots*."""
    if not allowed_roots:
        raise ValueError("allowed_roots must not be empty")

    if not raw:
        if default_to_first_root:
            candidate = allowed_roots[0]
        elif default_to_cwd:
            candidate = Path.cwd()
        else:
            raise ValueError(f"{label} is required")
    else:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate

    resolved = candidate.resolve(strict=False)
    for root in allowed_roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return resolved
        except ValueError:
            continue
    raise ValueError(f"{label} '{raw}' is outside the allowed roots")
