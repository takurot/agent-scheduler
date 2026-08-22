from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from subsched.github.conflict import (
    RebaseResult,
    RebaseStatus,
    handle_rebase_outcome,
    rebase_onto_base,
)
from subsched.models import Task, TaskState


@pytest.fixture
def git_repo_with_branch(tmp_path: Path) -> tuple[Path, Path]:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)

    readme = repo_dir / "README.md"
    readme.write_text("# Main Branch\nInitial content\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True)

    # Create feature branch worktree
    feature_dir = tmp_path / "worktree-101"
    subprocess.run(
        ["git", "worktree", "add", "-b", "subsched/issue-101", str(feature_dir), "main"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    return repo_dir, feature_dir


def test_rebase_clean(git_repo_with_branch: tuple[Path, Path]) -> None:
    repo_dir, feature_dir = git_repo_with_branch

    # Commit on main
    new_file = repo_dir / "other.txt"
    new_file.write_text("other\n", encoding="utf-8")
    subprocess.run(["git", "add", "other.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "main commit"], cwd=repo_dir, check=True)

    # Commit on feature
    feat_file = feature_dir / "feature.txt"
    feat_file.write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=feature_dir, check=True)
    subprocess.run(["git", "commit", "-m", "feature commit"], cwd=feature_dir, check=True)

    result = rebase_onto_base(feature_dir, base_branch="main")
    assert result.status == RebaseStatus.SUCCESS
    assert result.conflicted_files == ()


def test_rebase_conflict_detects_and_aborts(git_repo_with_branch: tuple[Path, Path]) -> None:
    repo_dir, feature_dir = git_repo_with_branch

    # Commit change to README on main
    readme_main = repo_dir / "README.md"
    readme_main.write_text("# Main Branch\nConflicting change from main\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "conflict on main"], cwd=repo_dir, check=True)

    # Commit conflicting change to README on feature
    readme_feat = feature_dir / "README.md"
    readme_feat.write_text("# Main Branch\nConflicting change from feature\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=feature_dir, check=True)
    subprocess.run(["git", "commit", "-m", "conflict on feature"], cwd=feature_dir, check=True)

    result = rebase_onto_base(feature_dir, base_branch="main")
    assert result.status == RebaseStatus.CONFLICT
    assert "README.md" in result.conflicted_files

    # Ensure rebase was cleanly aborted (git status is clean, not in rebase state)
    status_proc = subprocess.run(
        ["git", "status"],
        cwd=feature_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "rebase in progress" not in status_proc.stdout.casefold()


def test_rebase_invalid_branch(tmp_path: Path) -> None:
    # Rebase on non-repo directory fails safely
    result = rebase_onto_base(tmp_path, base_branch="non-existent")
    assert result.status in {RebaseStatus.FAILURE, RebaseStatus.CONFLICT}


def test_handle_rebase_outcome() -> None:
    task = Task(
        task_id="github-101",
        issue_number=101,
        title="Task 101",
        labels=(),
        status=TaskState.NEEDS_REBASE,
    )

    success_result = RebaseResult(status=RebaseStatus.SUCCESS)
    updated, msg = handle_rebase_outcome(task, success_result)
    assert updated.status == TaskState.READY
    assert "cleanly" in msg

    conflict_result = RebaseResult(
        status=RebaseStatus.CONFLICT, conflicted_files=("README.md",)
    )
    updated, msg = handle_rebase_outcome(task, conflict_result)
    assert updated.status == TaskState.NEEDS_HUMAN
    assert "conflict" in msg.casefold()

    fail_result = RebaseResult(status=RebaseStatus.FAILURE, output="fatal error")
    updated, msg = handle_rebase_outcome(task, fail_result)
    assert updated.status == TaskState.NEEDS_HUMAN
    assert "fatal error" in msg
