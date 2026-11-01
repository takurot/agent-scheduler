"""Safely materialize an already-authenticated audit cache in a private directory."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from subsched.audit_inventory import expected_cache_paths


def materialize_audit_cache(
    root: Path, cache: Mapping[str, bytes], inventory: Sequence[tuple[str, str]]
) -> None:
    """Write exact cache members; caller must authenticate provenance beforehand."""
    if (
        not root.is_absolute()
        or root.exists()
        or root.is_symlink()
        or any(parent.is_symlink() for parent in root.parents)
        or set(cache) != expected_cache_paths(inventory)
        or any(
            not isinstance(value, bytes) or not value or len(value) > 1024 * 1024
            for value in cache.values()
        )
        or sum(len(value) for value in cache.values()) > 16 * 1024 * 1024
    ):
        raise ValueError("invalid audit cache")
    root.mkdir(mode=0o700)
    for relative, contents in cache.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
