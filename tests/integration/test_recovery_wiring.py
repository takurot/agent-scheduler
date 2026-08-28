from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from subsched.contract import bootstrap_task_files
from subsched.models import (
    AgentResult,
    AgentResultKind,
    Capacity,
    CapacityState,
    Issue,
    Task,
    TaskState,
)
from subsched.recovery import ProcessRecord, load_process_record, save_process_record
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore, get_process_start_time

# Regression coverage for #164: recovery.py existed but was never wired into
# Scheduler.__init__/tick(), so a crashed worker permanently starved every other READY
# task (the crashed task's stale lease was re-registered forever and nothing ever
# released it). These tests reproduce the issue's exact repro scenario end-to-end.


def _available(agent: str) -> Capacity:
    return Capacity(
        agent=agent,
        state=CapacityState.AVAILABLE,
        observed_at=datetime.now(UTC),
        source="provider",
        confidence="high",
    )


def _crashed_in_progress_task(worktree: Path, issue_number: int) -> Task:
    """A task that was DISPATCHED/IN_PROGRESS when its worker process died, with a
    process record on disk pointing at a pid that is (near-certainly) dead."""
    worktree.mkdir(parents=True)
    task = (
        Task.from_issue(Issue(number=issue_number, title=f"Task {issue_number}"))
        .transition(TaskState.DISPATCHED, current_agent="claude")
        .transition(TaskState.IN_PROGRESS, current_agent="claude")
    )
    task = task.with_worktree(str(worktree))
    bootstrap_task_files(worktree, task)
    save_process_record(
        worktree,
        ProcessRecord(
            pid=999999,
            started_at="2026-08-22T00:00:00Z",
            agent="claude",
            issue_number=issue_number,
            worktree=str(worktree),
            attempt_nonce="nonce_crashed",
        ),
    )
    return task


def test_crashed_task_does_not_block_other_ready_tasks(tmp_path: Path) -> None:
    """Reproduces the issue's repro: issue #7 crashed mid-run (IN_PROGRESS), issue #8 is
    READY. With max_agent_failures=1, the crash-recovered #7 escalates straight to
    NEEDS_HUMAN, so #8 -- previously starved forever -- must be dispatched on the very
    next tick."""
    worktree_root = tmp_path / "worktrees"
    task7 = _crashed_in_progress_task(worktree_root / "issue-7", 7)
    task8 = Task.from_issue(Issue(number=8, title="Task 8"))

    store = JsonStateStore(tmp_path / "state.json")
    store.save_tasks((task7, task8))

    scheduler = Scheduler(
        store=store,
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker(
            {(8, "claude"): (AgentResult(AgentResultKind.FAILURE, output="boom"),)}
        ),
        worktree_root=worktree_root,
        max_agent_failures=1,
    )

    # Reconciled at construction time, before the stale lease could be re-registered.
    reconciled = {t.issue_number: t.status for t in scheduler.tasks}
    assert reconciled[7] is TaskState.NEEDS_HUMAN
    assert reconciled[8] is TaskState.READY
    crashed = next(t for t in scheduler.tasks if t.issue_number == 7)
    assert dict(crashed.per_agent_failures) == {"claude": 1}
    assert load_process_record(worktree_root / "issue-7", 7) is None
    assert scheduler.lease_manager.active_count == 0

    dispatched = scheduler.tick([_available("claude")])
    assert dispatched is True
    assert scheduler.worker.dispatches == [(8, "claude")]  # type: ignore[attr-defined]


def test_crashed_task_below_failure_threshold_recovers_to_ready(tmp_path: Path) -> None:
    """A first crash consumes one per-agent failure but remains below the default
    max_agent_failures=2 threshold, so recovery resolves RETRY -> READY."""
    worktree_root = tmp_path / "worktrees"
    task7 = _crashed_in_progress_task(worktree_root / "issue-7", 7)

    store = JsonStateStore(tmp_path / "state.json")
    store.save_tasks((task7,))

    scheduler = Scheduler(
        store=store,
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker(
            {(7, "claude"): (AgentResult(AgentResultKind.FAILURE, output="boom"),)}
        ),
        worktree_root=worktree_root,
    )

    assert scheduler.tasks[0].status is TaskState.READY
    assert dict(scheduler.tasks[0].per_agent_failures) == {"claude": 1}

    dispatched = scheduler.tick([_available("claude")])
    assert dispatched is True
    assert scheduler.worker.dispatches == [(7, "claude")]  # type: ignore[attr-defined]


def test_verification_failure_then_first_crash_does_not_escalate(tmp_path: Path) -> None:
    """A verification retry must not consume the crash-recovery Agent budget.

    With max_agent_failures=2, one prior verification failure plus the first Claude
    crash leaves Claude's own failure count at one and resolves recovery to READY even
    though the shared diagnostic attempt counter reaches two.
    """
    worktree_root = tmp_path / "worktrees"
    task = replace(
        _crashed_in_progress_task(worktree_root / "issue-13", 13),
        attempt=1,
        verification_failures=1,
        last_dispatched_agent="claude",
    )

    store = JsonStateStore(tmp_path / "state.json")
    store.save_tasks((task,))

    scheduler = Scheduler(
        store=store,
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({}),
        worktree_root=worktree_root,
        max_agent_failures=2,
    )

    recovered = scheduler.tasks[0]
    assert recovered.status is TaskState.READY
    assert recovered.attempt == 2
    assert recovered.verification_failures == 1
    assert dict(recovered.per_agent_failures) == {"claude": 1}


def test_crash_recovery_without_agent_identity_fails_closed(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    task = replace(
        _crashed_in_progress_task(worktree_root / "issue-14", 14),
        current_agent=None,
        last_dispatched_agent=None,
    )
    store = JsonStateStore(tmp_path / "state.json")
    store.save_tasks((task,))

    scheduler = Scheduler(
        store=store,
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({}),
        worktree_root=worktree_root,
    )

    recovered = scheduler.tasks[0]
    assert recovered.status is TaskState.NEEDS_HUMAN
    assert recovered.per_agent_failures == ()
    assert recovered.needs_human_reason is not None
    assert "Agent identity missing" in recovered.needs_human_reason


def test_crash_recovery_with_inconsistent_agent_identity_fails_closed(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    task = replace(
        _crashed_in_progress_task(worktree_root / "issue-15", 15),
        last_dispatched_agent="codex",
    )
    store = JsonStateStore(tmp_path / "state.json")
    store.save_tasks((task,))

    scheduler = Scheduler(
        store=store,
        router=Router(
            [AgentConfig("claude", priority=100), AgentConfig("codex", priority=90)]
        ),
        worker=ScriptedWorker({}),
        worktree_root=worktree_root,
    )

    recovered = scheduler.tasks[0]
    assert recovered.status is TaskState.NEEDS_HUMAN
    assert recovered.per_agent_failures == ()
    assert recovered.needs_human_reason is not None
    assert "Agent identity inconsistent" in recovered.needs_human_reason


def test_in_progress_task_with_no_worktree_escalates_to_needs_human(tmp_path: Path) -> None:
    """A DISPATCHED/IN_PROGRESS task with no worktree recorded at all cannot be checked
    against a process record or a handoff -- fail-closed to NEEDS_HUMAN rather than
    resuming blindly."""
    worktree_root = tmp_path / "worktrees"
    task = Task.from_issue(Issue(number=10, title="Task 10")).transition(
        TaskState.DISPATCHED, current_agent="claude"
    )
    task = task.transition(TaskState.IN_PROGRESS, current_agent="claude")
    assert task.worktree is None

    store = JsonStateStore(tmp_path / "state.json")
    store.save_tasks((task,))

    scheduler = Scheduler(
        store=store,
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({}),
        worktree_root=worktree_root,
    )

    reconciled = scheduler.tasks[0]
    assert reconciled.status is TaskState.NEEDS_HUMAN
    assert reconciled.needs_human_reason is not None
    assert scheduler.lease_manager.active_count == 0


def test_dispatched_task_with_no_worktree_escalates_without_crashing_startup(
    tmp_path: Path,
) -> None:
    """PR #178 review finding: DISPATCHED cannot transition directly to NEEDS_HUMAN
    (ALLOWED_TRANSITIONS in models.py), so a persisted task found at DISPATCHED with no
    worktree recorded (hand-edited or legacy state.json; unreachable via the live
    dispatch path, which always sets the worktree before persisting DISPATCHED) must not
    raise StateTransitionError out of Scheduler.__init__ -- that would abort the entire
    run for every task, not just this one."""
    worktree_root = tmp_path / "worktrees"
    task = Task.from_issue(Issue(number=12, title="Task 12")).transition(
        TaskState.DISPATCHED, current_agent="claude"
    )
    assert task.worktree is None

    store = JsonStateStore(tmp_path / "state.json")
    store.save_tasks((task,))

    scheduler = Scheduler(
        store=store,
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({}),
        worktree_root=worktree_root,
    )

    reconciled = scheduler.tasks[0]
    assert reconciled.status is TaskState.NEEDS_HUMAN
    assert reconciled.needs_human_reason is not None
    assert scheduler.lease_manager.active_count == 0


def test_still_live_process_is_left_untouched_on_startup(tmp_path: Path) -> None:
    """If the recorded process is genuinely still running, reconcile must not disturb
    the task or its lease -- a second dispatch of the same issue would violate
    concurrency=1."""
    worktree_root = tmp_path / "worktrees"
    worktree = worktree_root / "issue-11"
    worktree.mkdir(parents=True)
    task = (
        Task.from_issue(Issue(number=11, title="Task 11"))
        .transition(TaskState.DISPATCHED, current_agent="claude")
        .transition(TaskState.IN_PROGRESS, current_agent="claude")
    )
    task = task.with_worktree(str(worktree))
    bootstrap_task_files(worktree, task)
    save_process_record(
        worktree,
        ProcessRecord(
            pid=os.getpid(),
            started_at=get_process_start_time(os.getpid()) or "",
            agent="claude",
            issue_number=11,
            worktree=str(worktree),
            attempt_nonce="nonce_live",
        ),
    )

    store = JsonStateStore(tmp_path / "state.json")
    store.save_tasks((task,))

    scheduler = Scheduler(
        store=store,
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({}),
        worktree_root=worktree_root,
    )

    assert scheduler.tasks[0].status is TaskState.IN_PROGRESS
    assert scheduler.lease_manager.active_count == 1
    assert scheduler.lease_manager.is_agent_busy("claude") is True
    assert load_process_record(worktree, 11) is not None


def test_tick_writes_process_record_before_dispatch_and_clears_it_after(
    tmp_path: Path,
) -> None:
    """#164 acceptance: `.ai/runtime/<issue>.process.json` is created before the worker
    runs (so a future crash mid-dispatch leaves evidence to reconcile against) and
    removed again once the worker returns normally."""
    worktree_root = tmp_path / "worktrees"
    observed: dict[str, object] = {}

    class ObservingWorker:
        def run(self, task: Task, agent: str) -> AgentResult:
            record = load_process_record(Path(task.worktree or ""), task.issue_number)
            observed["record_during_run"] = record
            return AgentResult(AgentResultKind.FAILURE, output="boom")

    store = JsonStateStore(tmp_path / "state.json")
    store.save_tasks((Task.from_issue(Issue(number=9, title="Task 9")),))

    scheduler = Scheduler(
        store=store,
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ObservingWorker(),
        worktree_root=worktree_root,
    )

    scheduler.tick([_available("claude")])

    record = observed["record_during_run"]
    assert record is not None
    assert record.pid == os.getpid()  # type: ignore[union-attr]
    assert load_process_record(worktree_root / "issue-9", 9) is None
