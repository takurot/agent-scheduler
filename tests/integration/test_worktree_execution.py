from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from subsched.models import AgentResult, AgentResultKind, Capacity, CapacityState, Issue, TaskState
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore
from subsched.tasks.worktree import GitWorktreeAdapter, WorktreeConflictError


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


def test_registered_worktree_on_different_branch_is_rejected(
    git_repo: tuple[Path, Path],
) -> None:
    repo_dir, worktree_root = git_repo
    adapter = GitWorktreeAdapter(repo_dir, worktree_root)
    ctx = adapter.prepare_worktree(101)
    subprocess.run(
        ["git", "switch", "-c", "manual-maintenance"],
        cwd=ctx.path,
        check=True,
        capture_output=True,
    )

    with pytest.raises(WorktreeConflictError, match="expected branch subsched/issue-101"):
        adapter.prepare_worktree(101)


def test_registered_worktree_at_detached_head_is_rejected(
    git_repo: tuple[Path, Path],
) -> None:
    repo_dir, worktree_root = git_repo
    adapter = GitWorktreeAdapter(repo_dir, worktree_root)
    ctx = adapter.prepare_worktree(101)
    subprocess.run(
        ["git", "switch", "--detach"], cwd=ctx.path, check=True, capture_output=True
    )

    with pytest.raises(WorktreeConflictError, match="detached HEAD"):
        adapter.prepare_worktree(101)


def test_registered_worktree_validates_explicit_custom_branch(
    git_repo: tuple[Path, Path],
) -> None:
    repo_dir, worktree_root = git_repo
    adapter = GitWorktreeAdapter(repo_dir, worktree_root)

    first = adapter.prepare_worktree(102, branch="custom/issue-102")
    second = adapter.prepare_worktree(102, branch="custom/issue-102")

    assert second == first


def test_scheduler_escalates_worktree_identity_conflict_without_dispatch(
    git_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    repo_dir, worktree_root = git_repo
    adapter = GitWorktreeAdapter(repo_dir, worktree_root)
    ctx = adapter.prepare_worktree(101)
    subprocess.run(
        ["git", "switch", "-c", "manual-maintenance"],
        cwd=ctx.path,
        check=True,
        capture_output=True,
    )
    worker = ScriptedWorker(
        {(101, "claude"): (AgentResult(AgentResultKind.PASS),)}
    )
    store = JsonStateStore(tmp_path / "state")
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=worktree_root,
        worktree_adapter=adapter,
        push_enabled=True,
        create_pr_enabled=True,
        repo="owner/project",
        base_branch="main",
    )
    scheduler.discover((Issue(number=101, title="Task 101"),))
    now = datetime(2026, 8, 29, tzinfo=UTC)

    progressed = scheduler.tick(
        (
            Capacity(
                agent="claude",
                state=CapacityState.AVAILABLE,
                observed_at=now,
                source="provider",
                confidence="high",
            ),
        ),
        now=now,
    )

    task = scheduler.tasks[0]
    assert progressed is True
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.needs_human_reason == (
        "worktree preparation failed: WorktreeConflictError"
    )
    assert task.worktree is None
    assert worker.dispatches == []
    assert store.load_tasks() == scheduler.tasks
