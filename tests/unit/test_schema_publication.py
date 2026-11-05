"""Synthetic canaries for the host-side native schema publication boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from subsched.agents.codex import CODEX_OUTPUT_SCHEMA, ensure_codex_output_schema
from subsched.plan_review import PLAN_REVIEW_OUTPUT_SCHEMA, ensure_plan_review_output_schema

WRITERS = (ensure_codex_output_schema, ensure_plan_review_output_schema)


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("exists", [True, False])
def test_rejects_leaf_symlink(tmp_path: Path, writer: Any, exists: bool) -> None:
    outside = tmp_path / "outside"
    if exists:
        outside.write_text("canary")
    leaf = tmp_path / "schema.json"
    leaf.symlink_to(outside)
    with pytest.raises(OSError):
        writer(leaf)
    assert outside.exists() is exists
    if exists:
        assert outside.read_text() == "canary"
    assert leaf.is_symlink()


@pytest.mark.parametrize("writer", WRITERS)
def test_rejects_directory_symlink(tmp_path: Path, writer: Any) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        writer(link / "nested" / "schema.json")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_rejects_nonregular_target(tmp_path: Path, writer: Any, kind: str) -> None:
    leaf = tmp_path / "schema.json"
    if kind == "fifo":
        os.mkfifo(leaf)
    else:
        leaf.mkdir()
    with pytest.raises(OSError):
        writer(leaf)


@pytest.mark.parametrize("writer", WRITERS)
def test_rejects_parent_traversal(tmp_path: Path, writer: Any) -> None:
    with pytest.raises(OSError):
        writer(tmp_path / "new" / ".." / "schema.json")
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_DIRECTORY"])
def test_unsupported_primitives_fail_closed(
    tmp_path: Path,
    writer: Any,
    flag: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, flag)
    with pytest.raises(OSError):
        writer(tmp_path / "new" / "schema.json")
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("when", ["before", "after"])
def test_directory_swap_never_writes_outside(
    tmp_path: Path,
    writer: Any,
    when: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "schema.json").write_text("canary")
    original_open = os.open
    swapped = False

    def swap() -> None:
        nonlocal swapped
        parent.rename(tmp_path / "moved")
        parent.symlink_to(outside, target_is_directory=True)
        swapped = True

    def racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        match = path == "parent" and not swapped
        if match and when == "before":
            swap()
        fd = original_open(path, flags, *args, **kwargs)
        if match and when == "after":
            swap()
        return fd

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(OSError):
        writer(parent / "schema.json")
    assert swapped
    assert (outside / "schema.json").read_text() == "canary"
    assert list((tmp_path / "moved").iterdir()) == []


@pytest.mark.parametrize(
    "writer,schema,suffix",
    [
        (ensure_codex_output_schema, CODEX_OUTPUT_SCHEMA, "\n"),
        (ensure_plan_review_output_schema, PLAN_REVIEW_OUTPUT_SCHEMA, ""),
    ],
)
def test_canonical_repeated_generation(
    tmp_path: Path,
    writer: Any,
    schema: Any,
    suffix: str,
) -> None:
    leaf = tmp_path / "operator" / "nested" / "schema.json"
    for _ in range(2):
        result = writer(leaf)
        assert result == (leaf if writer is ensure_codex_output_schema else None)
        assert leaf.read_bytes() == (json.dumps(schema, indent=2) + suffix).encode()
    assert list(leaf.parent.iterdir()) == [leaf]


@pytest.mark.parametrize("writer", WRITERS)
def test_canonical_symlink_is_rejected_without_reading(
    tmp_path: Path,
    writer: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    writer(outside)
    canonical = outside.read_bytes()
    leaf = tmp_path / "schema.json"
    leaf.symlink_to(outside)

    def forbidden_read(*args: Any, **kwargs: Any) -> str:
        pytest.fail("schema writer must not read attacker-controlled targets")

    monkeypatch.setattr(Path, "read_text", forbidden_read)
    with pytest.raises(OSError):
        writer(leaf)
    assert outside.read_bytes() == canonical


@pytest.mark.parametrize("writer", WRITERS)
def test_hardlink_target_is_not_truncated(tmp_path: Path, writer: Any) -> None:
    outside = tmp_path / "outside"
    outside.write_text("canary")
    leaf = tmp_path / "schema.json"
    leaf.hardlink_to(outside)
    writer(leaf)
    assert outside.read_text() == "canary"
    assert leaf.read_text() != "canary"


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("operation", ["open", "mkdir", "stat", "replace"])
def test_runtime_unsupported_operations_fail_closed(
    tmp_path: Path,
    writer: Any,
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unsupported(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("synthetic unsupported primitive")

    leaf = tmp_path / "new" / "schema.json"
    with monkeypatch.context() as patch:
        patch.setattr(os, operation, unsupported)
        with pytest.raises(OSError):
            writer(leaf)
    assert not leaf.exists()
    if leaf.parent.exists():
        remaining = list(leaf.parent.iterdir())
        # No pathname-based error cleanup: a peer may replace any temporary name.
        assert len(remaining) == (1 if operation == "replace" else 0)
        assert all(entry.name.startswith(".schema-") for entry in remaining)


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("swap_at", ["fsync", "replace"])
def test_leaf_swap_during_publication_does_not_follow_target(
    tmp_path: Path,
    writer: Any,
    swap_at: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("canary")
    leaf = tmp_path / "schema.json"
    leaf.write_text("old schema")
    original = getattr(os, swap_at)
    swapped = False

    def racing_operation(*args: Any, **kwargs: Any) -> Any:
        nonlocal swapped
        if not swapped:
            leaf.unlink()
            leaf.symlink_to(outside)
            swapped = True
        return original(*args, **kwargs)

    monkeypatch.setattr(os, swap_at, racing_operation)
    if swap_at == "fsync":
        with pytest.raises(OSError):
            writer(leaf)
        assert leaf.is_symlink()
    else:
        writer(leaf)  # Atomic rename replaces the raced link; it never follows it.
        assert not leaf.is_symlink()
    assert swapped
    assert outside.read_text() == "canary"
    remaining = list(tmp_path.glob(".schema-*.tmp"))
    assert len(remaining) == (1 if swap_at == "fsync" else 0)
    assert sorted(p.name for p in tmp_path.iterdir() if p not in remaining) == [
        "outside", "schema.json"
    ]


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("kind", ["symlink", "regular", "fifo", "inplace"])
@pytest.mark.parametrize("when", ["fsync", "replace", "after_replace"])
def test_temporary_source_substitution_fails_closed(
    tmp_path: Path,
    writer: Any,
    kind: str,
    when: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("canary")
    leaf = tmp_path / "schema.json"
    leaf.write_text("old schema")
    original_fsync = os.fsync
    original_replace = os.replace
    swapped = False
    temporary: Path | None = None

    def swap(source: Path) -> None:
        nonlocal swapped, temporary
        temporary = source
        if kind == "inplace":
            source.write_bytes(b'{"attacker": true}')
        else:
            source.unlink()
            if kind == "symlink":
                source.symlink_to(outside)
            elif kind == "fifo":
                os.mkfifo(source)
            else:
                source.write_bytes(b'{"attacker": true}')
        swapped = True

    def racing_fsync(fd: int) -> None:
        original_fsync(fd)
        if when == "fsync" and not swapped:
            swap(next(tmp_path.glob(".schema-*.tmp")))

    def racing_replace(src: str, dst: str, **kwargs: Any) -> None:
        if when == "replace":
            swap(tmp_path / src)
        original_replace(src, dst, **kwargs)
        if when == "after_replace":
            swap(leaf)

    monkeypatch.setattr(os, "fsync", racing_fsync)
    monkeypatch.setattr(os, "replace", racing_replace)
    with pytest.raises(OSError):
        writer(leaf)
    assert swapped
    assert outside.read_text() == "canary"
    if when == "fsync":
        assert leaf.read_text() == "old schema"
    # Failure must preserve a substituted source; do not unlink an untrusted name.
    assert temporary is not None
    replacement = temporary if when == "fsync" else leaf
    assert replacement.lstat()
    if kind == "symlink":
        assert replacement.is_symlink()
    elif kind != "fifo":
        assert replacement.read_bytes() == b'{"attacker": true}'


@pytest.mark.parametrize("writer", WRITERS)
def test_failed_replace_preserves_replacement_temporary_entry(
    tmp_path: Path, writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replacement: Path | None = None

    def failing_replace(src: str, dst: str, **kwargs: Any) -> None:
        nonlocal replacement
        replacement = tmp_path / src
        replacement.unlink()
        replacement.write_text("peer entry")
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        writer(tmp_path / "schema.json")
    assert replacement is not None
    assert replacement.read_text() == "peer entry"
