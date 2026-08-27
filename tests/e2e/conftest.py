from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from subsched.gitenv import git_safe_env

# tests/e2e/conftest.py -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
        env=git_safe_env(),
    )
    return result.stdout.strip()


def _repo_health(path: Path) -> tuple[str, str, str, str]:
    """Snapshot the parts of `path`'s git state that an e2e test must never touch."""
    return (
        _git(path, "config", "--get", "core.bare"),
        _git(path, "config", "--get", "user.email"),
        _git(path, "config", "--get", "user.name"),
        _git(path, "worktree", "list", "--porcelain"),
    )


@pytest.fixture(autouse=True)
def _guard_real_repository_git_state() -> Iterator[None]:
    """Fail loudly if an e2e test mutates *this* repository's `.git` state (issue #147).

    E2E tests exercise real `git` subprocesses against isolated `tmp_path` repositories, but a
    leaked repository-discovery environment variable (`GIT_DIR`, `GIT_WORK_TREE`, ...) or an
    incorrectly resolved `repo_root` can silently redirect those operations onto the checkout
    running the test suite instead of the isolated fixture repository. This guard snapshots the
    invariant parts of this repository's own `.git` state before and after every e2e test and
    fails immediately if anything changed, rather than relying on someone noticing `git status`
    behaving strangely afterward.
    """
    if not (_REPO_ROOT / ".git").exists():
        # Not a git checkout (e.g. an extracted sdist/tarball) -- nothing to guard.
        yield
        return

    before = _repo_health(_REPO_ROOT)
    yield
    after = _repo_health(_REPO_ROOT)
    assert before == after, (
        "an e2e test mutated this repository's own .git state "
        "(core.bare / user.email / user.name / worktree registrations changed): "
        f"before={before!r} after={after!r}"
    )
