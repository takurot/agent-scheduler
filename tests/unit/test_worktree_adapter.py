from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from subsched.gitenv import GIT_LOCATION_OVERRIDE_VARS
from subsched.tasks.worktree import (
    GitWorktreeAdapter,
    InvalidRepositoryError,
    WorktreeConflictError,
    WorktreeSecurityError,
)


def test_rejects_non_git_repo(tmp_path: Path) -> None:
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    worktree_root = tmp_path / "worktrees"

    with pytest.raises(InvalidRepositoryError, match="not a valid git repository"):
        GitWorktreeAdapter(non_repo, worktree_root)


def test_rejects_nonexistent_repo_dir(tmp_path: Path) -> None:
    with pytest.raises(InvalidRepositoryError, match="not a directory"):
        GitWorktreeAdapter(tmp_path / "does-not-exist", tmp_path / "worktrees")


def test_rejects_invalid_issue_numbers(tmp_path: Path) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")

    adapter = GitWorktreeAdapter(tmp_path, tmp_path / "worktrees", run=fake_run)

    with pytest.raises(ValueError, match="invalid issue number"):
        adapter.get_branch_name(0)

    with pytest.raises(ValueError, match="invalid issue number"):
        adapter.get_worktree_path(-1)


def test_security_rejects_symlinks_and_escaping_paths(tmp_path: Path) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")

    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    adapter = GitWorktreeAdapter(tmp_path, worktree_root, run=fake_run)

    symlink_path = worktree_root / "issue-42"
    real_target = tmp_path / "other"
    real_target.mkdir()
    symlink_path.symlink_to(real_target)

    with pytest.raises(WorktreeSecurityError, match="is a symlink"):
        adapter.validate_worktree_path(42, symlink_path)


def test_rejects_unregistered_existing_directory(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    target_path = worktree_root / "issue-1"
    target_path.mkdir()

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if "worktree" in cmd and "list" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="worktree /some/other\n\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    adapter = GitWorktreeAdapter(tmp_path, worktree_root, run=fake_run)

    with pytest.raises(WorktreeConflictError, match="not registered as a git worktree"):
        adapter.prepare_worktree(1)


def test_all_git_invocations_strip_location_override_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every `git` subprocess call must pass an env stripped of GIT_DIR & co. (issue #147):
    otherwise a leaked GIT_DIR/GIT_WORK_TREE silently redirects `-C <repo_root>` operations
    onto an unrelated repository."""
    monkeypatch.setenv("GIT_DIR", "/leaked/.git")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if "worktree" in cmd and "list" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "show-ref" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    worktree_root = tmp_path / "worktrees"
    adapter = GitWorktreeAdapter(tmp_path, worktree_root, run=fake_run)
    adapter.prepare_worktree(1)

    assert calls, "expected at least one git invocation"
    for _cmd, kwargs in calls:
        env = kwargs.get("env")
        assert isinstance(env, dict), "every git call must pass an explicit env"
        for name in GIT_LOCATION_OVERRIDE_VARS:
            assert name not in env
