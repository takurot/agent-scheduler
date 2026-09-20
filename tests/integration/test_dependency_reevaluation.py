"""#374: dependency-originated BLOCKED tasks must be re-evaluated when their
dependencies recover or complete -- while self-dependency and cycle blocks stay."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

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


def _available() -> Capacity:
    return Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=datetime.now(UTC),
        source="provider",
        confidence="high",
    )


def _scheduler(store: JsonStateStore, worktree_root: Path) -> Scheduler:
    return Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=ScriptedWorker({}),
        worktree_root=worktree_root,
    )


def _issue(number: int, body: str = "") -> Issue:
    return Issue(number=number, title=f"Issue {number}", body=body)


def test_blocked_child_recovers_when_parent_recovers_and_completes(tmp_path: Path) -> None:
    """Acceptance: parent NEEDS_HUMAN -> recovered -> COMPLETE must bring the
    dependency-originated BLOCKED child to WAITING_DEPENDENCY and then READY."""
    store = JsonStateStore(tmp_path / "state")
    parent = Task.from_issue(_issue(1)).transition(TaskState.NEEDS_HUMAN, reason="operator")
    child = replace(
        Task.from_issue(_issue(2, "Blocked-By: #1")),
        status=TaskState.BLOCKED,
    )
    store.save_tasks((parent, child))

    scheduler = _scheduler(store, tmp_path / "worktrees")
    scheduler.tick([])
    assert scheduler.queue.get(2).status is TaskState.BLOCKED

    # Operator resolves the parent (this is what resolve_needs_human persists).
    recovered_parent = parent.transition(TaskState.READY)
    store.save_tasks((recovered_parent, scheduler.queue.get(2)))

    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)}),
        worktree_root=tmp_path / "worktrees",
        verification_commands=("true",),
    )
    scheduler.tick([_available()])

    parent_after = scheduler.queue.get(1)
    child_after = scheduler.queue.get(2)
    assert parent_after.status is TaskState.COMPLETE
    assert child_after.status is TaskState.READY


def test_blocked_child_released_when_unknown_parent_is_discovered(tmp_path: Path) -> None:
    """Acceptance: a child blocked because its parent was unknown must leave BLOCKED
    once the parent is discovered (back to WAITING_DEPENDENCY, not dispatched)."""
    store = JsonStateStore(tmp_path / "state")
    scheduler = _scheduler(store, tmp_path / "worktrees")

    scheduler.discover((_issue(2, "Blocked-By: #1"),))
    assert scheduler.queue.get(2).status is TaskState.BLOCKED

    scheduler.discover((_issue(1), _issue(2, "Blocked-By: #1")))
    assert scheduler.queue.get(2).status is TaskState.WAITING_DEPENDENCY


def test_blocked_child_becomes_ready_when_parent_completes_on_restart(tmp_path: Path) -> None:
    """Acceptance: after a restart with the parent already COMPLETE, a BLOCKED child
    must be released and dispatchable again (dispatched to completion here)."""
    store = JsonStateStore(tmp_path / "state")
    parent = replace(Task.from_issue(_issue(1)), status=TaskState.COMPLETE)
    child = replace(Task.from_issue(_issue(2, "Blocked-By: #1")), status=TaskState.BLOCKED)
    store.save_tasks((parent, child))

    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=ScriptedWorker({(2, "claude"): (AgentResult(AgentResultKind.PASS),)}),
        worktree_root=tmp_path / "worktrees",
        verification_commands=("true",),
    )
    assert scheduler.tick([_available()]) is True

    assert scheduler.queue.get(2).status is TaskState.COMPLETE


def test_cycle_blocked_tasks_are_not_released(tmp_path: Path) -> None:
    """Guard: cycle members keep blocking each other -- re-evaluation must not
    release a BLOCKED task whose dependency is itself BLOCKED."""
    store = JsonStateStore(tmp_path / "state")
    scheduler = _scheduler(store, tmp_path / "worktrees")

    scheduler.discover((_issue(1, "Blocked-By: #2"), _issue(2, "Blocked-By: #1")))
    assert scheduler.queue.get(1).status is TaskState.BLOCKED
    assert scheduler.queue.get(2).status is TaskState.BLOCKED

    scheduler.tick([])
    assert scheduler.queue.get(1).status is TaskState.BLOCKED
    assert scheduler.queue.get(2).status is TaskState.BLOCKED


def test_self_dependency_block_is_not_released(tmp_path: Path) -> None:
    """Guard: a self-dependency block is structural; only an issue-body edit on
    discovery may clear it, never dependency re-evaluation."""
    store = JsonStateStore(tmp_path / "state")
    scheduler = _scheduler(store, tmp_path / "worktrees")

    scheduler.discover((_issue(3, "Blocked-By: #3"),))
    assert scheduler.queue.get(3).status is TaskState.BLOCKED

    scheduler.tick([])
    assert scheduler.queue.get(3).status is TaskState.BLOCKED
