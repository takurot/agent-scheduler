from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from subsched.models import Task, TaskState
from subsched.review import git_head_commit, review_report_path
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore


def _init_repo(repo_dir: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True)


def _write_report(worktree_dir: Path, issue_number: int, round_number: int, verdict: str) -> None:
    report = review_report_path(worktree_dir, issue_number, round_number)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        f"# Review\n\n## Verdict\n{verdict}\n\n## Summary\nok\n\n## Findings\nnone\n",
        encoding="utf-8",
    )


def _scheduler(tmp_path: Path) -> Scheduler:
    return Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=ScriptedWorker({}),
        worktree_root=tmp_path / "worktrees",
    )


def test_process_pr_review_approve_updates_review_cycles(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    _init_repo(tmp_path)
    _write_report(tmp_path, issue_number=1, round_number=1, verdict="APPROVE")

    scheduler = _scheduler(tmp_path)
    task = Task(
        task_id="github-1",
        issue_number=1,
        title="test",
        labels=(),
        status=TaskState.IN_PROGRESS,
        dispatch_status=TaskState.PR_REVIEW,
        worktree=str(tmp_path),
        review_cycles=0,
    )
    scheduler.queue = scheduler.queue.append((task,))

    scheduler._process_pr_review(task, "claude", now)

    final = scheduler.queue.get(1)
    assert final is not None
    assert final.review_cycles == 1
    assert final.status is TaskState.READY_FOR_REVIEW


def test_process_pr_review_fails_closed_when_head_moved(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    _init_repo(tmp_path)
    pre_dispatch_head = git_head_commit(tmp_path)
    _write_report(tmp_path, issue_number=1, round_number=1, verdict="APPROVE")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "sneaky commit"], cwd=tmp_path, check=True)

    scheduler = _scheduler(tmp_path)
    task = Task(
        task_id="github-1",
        issue_number=1,
        title="test",
        labels=(),
        status=TaskState.IN_PROGRESS,
        dispatch_status=TaskState.PR_REVIEW,
        worktree=str(tmp_path),
        review_cycles=0,
    )
    scheduler.queue = scheduler.queue.append((task,))

    scheduler._process_pr_review(task, "claude", now, pre_dispatch_head=pre_dispatch_head)

    final = scheduler.queue.get(1)
    assert final is not None
    assert final.status is TaskState.NEEDS_HUMAN
