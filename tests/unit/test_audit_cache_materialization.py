from __future__ import annotations

from pathlib import Path

import pytest

from subsched.audit_cache import materialize_audit_cache
from subsched.audit_inventory import expected_cache_paths


def test_materializes_only_exact_approved_cache(tmp_path: Path) -> None:
    items = [("packaging", "26.3")]
    key = next(iter(expected_cache_paths(items)))
    root = tmp_path / "cache"
    materialize_audit_cache(root, {key: b"verified data"}, items)
    assert (root / key).read_bytes() == b"verified data"
    assert (root / key).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("problem", ["missing", "extra", "empty", "existing", "symlink"])
def test_rejects_unsafe_or_incomplete_cache(tmp_path: Path, problem: str) -> None:
    items = [("packaging", "26.3")]
    key = next(iter(expected_cache_paths(items)))
    cache = {key: b"verified data"}
    root = tmp_path / "cache"
    if problem == "missing":
        cache = {}
    elif problem == "extra":
        cache["../../escape"] = b"bad"
    elif problem == "empty":
        cache[key] = b""
    elif problem == "existing":
        root.mkdir()
    else:
        root.symlink_to(tmp_path)
    with pytest.raises((OSError, ValueError)):
        materialize_audit_cache(root, cache, items)
    assert not (tmp_path.parent / "escape").exists()
