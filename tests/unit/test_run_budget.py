"""#375: execution.max_tasks_per_run is a per-run dispatch budget over distinct
issues -- never a cap on the persisted history (COMPLETE/NEEDS_HUMAN tasks)."""

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


def test_discover_allows_history_beyond_max_tasks_per_run(tmp_path: Path) -> None:
    """Acceptance: 50 persisted COMPLETE tasks must not stop a new issue from being
    discovered and dispatched (the old code raised 'task limit exceeded')."""
    history = tuple(
        replace(Task.from_issue(Issue(number=n, title=f"Done {n}")), status=TaskState.COMPLETE)
        for n in range(1, 51)
    )
    store = JsonStateStore(tmp_path / "state")
    store.save_tasks(history)

    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=ScriptedWorker({(51, "claude"): (AgentResult(AgentResultKind.PASS),)}),
        worktree_root=tmp_path / "worktrees",
        verification_commands=("true",),
        max_tasks_per_run=50,
    )
    scheduler.discover((Issue(number=51, title="New work"),))

    assert scheduler.queue.get(51).status is TaskState.READY
    assert scheduler.tick([_available()]) is True
    assert scheduler.queue.get(51).status is TaskState.COMPLETE


def test_run_budget_limits_distinct_issues_dispatched(tmp_path: Path) -> None:
    """Acceptance: with max_tasks_per_run=2 and 3 READY issues, exactly 2 distinct
    issues are dispatched; the third stays READY (not WAITING_CAPACITY) once the
    budget is exhausted."""
    store = JsonStateStore(tmp_path / "state")
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=ScriptedWorker(
            {
                (1, "claude"): (AgentResult(AgentResultKind.PASS),),
                (2, "claude"): (AgentResult(AgentResultKind.PASS),),
            }
        ),
        worktree_root=tmp_path / "worktrees",
        verification_commands=("true",),
        max_tasks_per_run=2,
    )
    scheduler.discover(
        (Issue(number=1, title="A"), Issue(number=2, title="B"), Issue(number=3, title="C"))
    )

    assert scheduler.tick([_available()]) is True
    assert scheduler.tick([_available()]) is True
    assert scheduler.queue.get(1).status is TaskState.COMPLETE
    assert scheduler.queue.get(2).status is TaskState.COMPLETE

    # Budget exhausted: no third dispatch, and the untouched task stays READY.
    assert scheduler.tick([_available()]) is False
    assert scheduler.queue.get(3).status is TaskState.READY


def test_retry_and_verification_retry_do_not_consume_budget(tmp_path: Path) -> None:
    """Acceptance: retries and verification retries of the same issue are the same
    budget slot -- only distinct issues count toward max_tasks_per_run."""
    store = JsonStateStore(tmp_path / "state")
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=ScriptedWorker(
            {
                (1, "claude"): (
                    AgentResult(AgentResultKind.FAILURE, output="first attempt fails"),
                    AgentResult(AgentResultKind.PASS),
                ),
            }
        ),
        worktree_root=tmp_path / "worktrees",
        verification_commands=("true",),
        max_agent_failures=3,
        max_tasks_per_run=1,
    )
    scheduler.discover((Issue(number=1, title="A"), Issue(number=2, title="B")))

    # First dispatch: agent failure -> RETRY -> READY (same issue, one budget slot).
    assert scheduler.tick([_available()]) is True
    assert scheduler.queue.get(1).status is TaskState.READY

    # Second dispatch of the same issue completes within the single-slot budget.
    assert scheduler.tick([_available()]) is True
    assert scheduler.queue.get(1).status is TaskState.COMPLETE

    # Issue 2 must not be dispatched: the 1-slot budget is spent on issue 1.
    assert scheduler.tick([_available()]) is False
    assert scheduler.queue.get(2).status is TaskState.READY
