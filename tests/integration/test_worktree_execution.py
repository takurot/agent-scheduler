from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from subsched.tasks.worktree import GitWorktreeAdapter


@pytest.fixture
def git_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    readme = repo_dir / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True)

    worktree_root = tmp_path / "worktrees"
    return repo_dir, worktree_root


def test_worktree_creates_and_preserves_dirty_changes_across_agents(
    git_repo: tuple[Path, Path],
) -> None:
    repo_dir, worktree_root = git_repo
    adapter = GitWorktreeAdapter(repo_dir, worktree_root)

    ctx1 = adapter.prepare_worktree(101)
    assert ctx1.path.exists()
    assert ctx1.branch == "subsched/issue-101"
    assert ctx1.issue_number == 101

    # Worker 1 creates uncommitted dirty change
    dirty_file = ctx1.path / "feature.py"
    dirty_file.write_text("print('in progress by worker 1')\n", encoding="utf-8")

    # Agent switch: Worker 2 prepares same worktree
    ctx2 = adapter.prepare_worktree(101)
    assert ctx2.path == ctx1.path

    # Worker 2 sees the exact same dirty file
    assert (
        (ctx2.path / "feature.py").read_text(encoding="utf-8")
        == "print('in progress by worker 1')\n"
    )

    # Branch and HEAD match
    branch_proc = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=ctx2.path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branch_proc.stdout.strip() == "subsched/issue-101"
