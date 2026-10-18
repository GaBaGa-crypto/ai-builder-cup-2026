"""Shared allow-list guard for agent-exposed data paths."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Union


def resolve_under_roots(path: Union[str, os.PathLike[str]],
                        roots: Iterable[Union[str, os.PathLike[str]]]) -> Path:
    """Resolve *path* and require it below one of *roots* when the data guard is enabled."""
    candidate = Path(path)
    if not os.environ.get("NORMALIZER_DATA_ROOT"):
        return candidate
    try:
        resolved = candidate.resolve()
        allowed = [Path(root).resolve() for root in roots]
        if not any(_contains(root, resolved) for root in allowed):
            raise ValueError
        return resolved
    except (OSError, RuntimeError, ValueError):
        name = candidate.name or "path"
        raise ValueError(f"path not allowed: {name}") from None


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_under_data_root(path: Union[str, os.PathLike[str]]) -> Path:
    """Resolve *path* and require it below NORMALIZER_DATA_ROOT when configured."""
    data_root = os.environ.get("NORMALIZER_DATA_ROOT")
    if not data_root:
        return Path(path)
    return resolve_under_roots(path, (data_root,))


def redact_data_root(value: object) -> str:
    """Replace the configured resolved data-root prefix in error text."""
    text = str(value)
    data_root = os.environ.get("NORMALIZER_DATA_ROOT")
    if not data_root:
        return text
    try:
        root = Path(data_root).resolve()
    except (OSError, RuntimeError, ValueError):
        return text
    for prefix in (str(root).replace("\\", "\\\\"), str(root), root.as_posix()):
        text = text.replace(prefix, "<data>")
    return text
