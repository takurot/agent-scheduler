from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from subsched.models import Issue, Task, TaskState
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore


def _scheduler(store: JsonStateStore, worktree_root: Path) -> Scheduler:
    return Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=ScriptedWorker({}),
        worktree_root=worktree_root,
    )


def test_reconcile_self_dependency_blocks_task(tmp_path: Path) -> None:
    """A self-dependency introduced in the issue body transitions the task to BLOCKED."""
    store = JsonStateStore(tmp_path / "state")
    scheduler = _scheduler(store, tmp_path / "worktrees")
    scheduler.discover((Issue(number=10, title="Self loop"),))

    assert scheduler.queue.get(10).status is TaskState.READY

    scheduler.discover(
        (Issue(number=10, title="Self loop", body="Blocked-By: #10"),),
        snapshot_complete=True,
    )
    assert scheduler.queue.get(10).status is TaskState.BLOCKED


def test_reconcile_in_flight_tasks_not_mutated(tmp_path: Path) -> None:
    """Tasks currently in-flight (DISPATCHED/IN_PROGRESS) must not be mutated silently."""
    store = JsonStateStore(tmp_path / "state")
    running = replace(
        Task.from_issue(Issue(number=20, title="In progress task")),
        status=TaskState.IN_PROGRESS,
    )
    store.save_tasks((running,))

    scheduler = _scheduler(store, tmp_path / "worktrees")
    scheduler.queue = scheduler.queue.replace(running)
    scheduler.discover(
        (Issue(number=20, title="Changed title", labels=("security-sensitive",)),),
        snapshot_complete=True,
    )

    task = scheduler.queue.get(20)
    assert task.status is TaskState.IN_PROGRESS
    assert task.title == "In progress task"


def test_partial_snapshot_does_not_escalate_missing_task(tmp_path: Path) -> None:
    """When snapshot_complete is False, missing issues remain in their current state."""
    store = JsonStateStore(tmp_path / "state")
    scheduler = _scheduler(store, tmp_path / "worktrees")
    scheduler.discover((Issue(number=30, title="Keep me"),))

    scheduler.discover((), snapshot_complete=False)

    task = scheduler.queue.get(30)
    assert task.status is TaskState.READY
    assert task.needs_human_reason is None
