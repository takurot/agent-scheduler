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


def test_restart_refreshes_existing_issue_metadata_without_duplicate(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path / "state")
    first = _scheduler(store, tmp_path / "worktrees")
    first.discover((Issue(number=1, title="Old", body="Old body", labels=("old",)),))

    restarted = _scheduler(store, tmp_path / "worktrees")
    restarted.discover((Issue(number=1, title="New", body="New body", labels=("new",)),))

    assert len(restarted.tasks) == 1
    task = restarted.tasks[0]
    assert task.title == "New"
    assert task.description == "New body"
    assert task.labels == ("new",)
    assert store.load_tasks() == restarted.tasks


def test_complete_snapshot_escalates_missing_ready_issue(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path / "state")
    scheduler = _scheduler(store, tmp_path / "worktrees")
    scheduler.discover((Issue(number=1, title="Open"),))

    scheduler.discover((), snapshot_complete=True)

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.needs_human_reason == "issue missing from complete GitHub snapshot"


def test_existing_issue_with_excluded_label_is_escalated(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path / "state")
    scheduler = _scheduler(store, tmp_path / "worktrees")
    scheduler.discover((Issue(number=1, title="Safe"),))

    scheduler.discover(
        (Issue(number=1, title="Sensitive", labels=("security-sensitive",)),),
        snapshot_complete=True,
    )

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.title == "Sensitive"
    assert task.needs_human_reason == "issue is no longer eligible: excluded label"


def test_dependency_changes_recompute_waiting_and_ready_state(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path / "state")
    scheduler = _scheduler(store, tmp_path / "worktrees")
    scheduler.discover((Issue(number=1, title="Root"), Issue(number=2, title="Child")))

    scheduler.discover(
        (
            Issue(number=1, title="Root"),
            Issue(number=2, title="Child", body="Blocked-By: #1"),
        ),
        snapshot_complete=True,
    )
    assert scheduler.queue.get(2).status is TaskState.WAITING_DEPENDENCY
    assert scheduler.queue.get(2).dependencies == (1,)

    scheduler.discover(
        (Issue(number=1, title="Root"), Issue(number=2, title="Child")),
        snapshot_complete=True,
    )
    assert scheduler.queue.get(2).status is TaskState.READY
    assert scheduler.queue.get(2).dependencies == ()


def test_terminal_task_is_not_mutated_by_new_snapshot(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path / "state")
    terminal = replace(
        Task.from_issue(Issue(number=1, title="Original")),
        status=TaskState.COMPLETE,
    )
    store.save_tasks((terminal,))
    scheduler = _scheduler(store, tmp_path / "worktrees")

    scheduler.discover(
        (Issue(number=1, title="Changed", labels=("security-sensitive",)),),
        snapshot_complete=True,
    )

    assert scheduler.tasks == (terminal,)
