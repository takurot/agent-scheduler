from __future__ import annotations

import concurrent.futures
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from subsched.events import FakeClock
from subsched.models import (
    AgentResult,
    AgentResultKind,
    Capacity,
    CapacityState,
    Issue,
    Task,
    TaskState,
)
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore
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


def test_concurrent_worktrees_isolated(git_repo: tuple[Path, Path]) -> None:
    repo_dir, worktree_root = git_repo
    adapter = GitWorktreeAdapter(repo_dir, worktree_root)

    ctx1 = adapter.prepare_worktree(101)
    ctx2 = adapter.prepare_worktree(102)

    assert ctx1.path != ctx2.path
    assert ctx1.branch == "subsched/issue-101"
    assert ctx2.branch == "subsched/issue-102"

    # Modify file in ctx1
    (ctx1.path / "file1.txt").write_text("content 1", encoding="utf-8")
    # Verify not present in ctx2
    assert not (ctx2.path / "file1.txt").exists()

    # Modify file in ctx2
    (ctx2.path / "file2.txt").write_text("content 2", encoding="utf-8")
    # Verify not present in ctx1
    assert not (ctx1.path / "file2.txt").exists()


def test_concurrent_state_store_updates_under_lock(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path / "state.json")
    store.init_directories()

    def update_task_worker(worker_id: int) -> None:
        with store.lock():
            current_tasks = list(store.load_tasks())
            t = Task(
                task_id=f"github-{worker_id}",
                issue_number=worker_id,
                title=f"Task {worker_id}",
                labels=(),
                status=TaskState.READY,
            )
            current_tasks.append(t)
            store.save_tasks(current_tasks)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(update_task_worker, i) for i in range(1, 9)]
        concurrent.futures.wait(futures)

    loaded = store.load_tasks()
    assert len(loaded) == 8
    issue_numbers = {task.issue_number for task in loaded}
    assert issue_numbers == set(range(1, 9))


def test_scheduler_concurrency_execution(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    clock = FakeClock(now)
    store = JsonStateStore(tmp_path / "state.json")
    router = Router([AgentConfig("claude", priority=100), AgentConfig("codex", priority=90)])

    worker = ScriptedWorker(
        {
            (101, "claude"): (AgentResult(AgentResultKind.PASS),),
            (102, "claude"): (AgentResult(AgentResultKind.PASS),),
            (102, "codex"): (AgentResult(AgentResultKind.PASS),),
        }
    )

    scheduler = Scheduler(
        store=store,
        router=router,
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        concurrency=2,
    )
    scheduler.discover([Issue(number=101, title="Task 101"), Issue(number=102, title="Task 102")])

    cap_claude = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        observed_at=now,
        source="provider",
        confidence="high",
    )
    cap_codex = Capacity(
        agent="codex",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        observed_at=now,
        source="provider",
        confidence="high",
    )

    capacities = [cap_claude, cap_codex]

    # First tick: runs issue 101 on claude
    ran1 = scheduler.tick(capacities, now=now)
    assert ran1 is True
    assert scheduler.tasks[0].status == TaskState.COMPLETE

    # Second tick: runs issue 102
    ran2 = scheduler.tick(capacities, now=now)
    assert ran2 is True
    assert scheduler.tasks[1].status == TaskState.COMPLETE
