from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


def _available(agent: str, *, observed_at: datetime | None = None) -> Capacity:
    return Capacity(
        agent=agent,
        state=CapacityState.AVAILABLE,
        observed_at=observed_at or datetime.now(UTC),
        source="provider",
        confidence="high",
    )


def _scheduler(tmp_path: Path, *, max_task_runtime_seconds: float | None) -> Scheduler:
    # A FAILURE result returns the task to READY (attempt 1 < default max_agent_failures
    # 2), so repeated ticks can keep re-dispatching the same task without ever reaching a
    # terminal state -- exactly the "still active, budget accumulating" scenario #137
    # needs to test.
    return Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker(
            {(101, "claude"): (AgentResult(AgentResultKind.FAILURE, output="boom"),) * 5}
        ),
        worktree_root=tmp_path / "worktrees",
        max_task_runtime_seconds=max_task_runtime_seconds,
        # High enough that max_agent_failures-driven escalation never interferes with
        # what these tests are actually exercising (the max_task_runtime budget).
        max_agent_failures=100,
    )


def test_run_started_at_is_set_on_first_dispatch_and_preserved(tmp_path: Path) -> None:
    """Regression test for #137: run_started_at must survive a retry (the task returns
    to READY, not a terminal state) so the budget is cumulative, not reset per attempt."""
    scheduler = _scheduler(tmp_path, max_task_runtime_seconds=None)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude", observed_at=start)], now=start)

    task = scheduler.tasks[0]
    assert task.status is TaskState.READY  # retried after FAILURE
    assert task.run_started_at == start

    # A second dispatch, later in wall-clock time, must not reset run_started_at.
    later = start + timedelta(hours=1)
    scheduler.tick([_available("claude", observed_at=later)], now=later)
    assert scheduler.tasks[0].run_started_at == start


def test_task_is_not_dispatched_once_max_task_runtime_exceeded(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path, max_task_runtime_seconds=3600.0)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude", observed_at=start)], now=start)
    assert scheduler.tasks[0].status is TaskState.READY

    # Well past the 1h budget: the next tick must expire the task instead of
    # re-dispatching it.
    later = start + timedelta(hours=2)
    scheduler.tick([_available("claude", observed_at=later)], now=later)

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.needs_human_reason is not None
    assert "max_task_runtime" in task.needs_human_reason
    # Only one attempt was ever actually dispatched -- the second tick expired the task
    # instead of calling the worker again.
    assert scheduler.worker.dispatches == [(101, "claude")]  # type: ignore[attr-defined]


def test_task_within_budget_is_not_expired(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path, max_task_runtime_seconds=3600.0)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude", observed_at=start)], now=start)

    soon = start + timedelta(minutes=30)
    scheduler.tick([_available("claude", observed_at=soon)], now=soon)

    assert scheduler.tasks[0].status is TaskState.READY
    assert len(scheduler.worker.dispatches) == 2  # type: ignore[attr-defined]


def test_no_budget_configured_never_expires_a_task(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path, max_task_runtime_seconds=None)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude", observed_at=start)], now=start)

    far_future = start + timedelta(days=365)
    scheduler.tick([_available("claude", observed_at=far_future)], now=far_future)

    assert scheduler.tasks[0].status is not TaskState.NEEDS_HUMAN


def test_run_started_at_survives_disk_reload_and_crash_recovery(tmp_path: Path) -> None:
    """Regression test for #137 code review: run_started_at must actually survive a real
    process restart -- not just an in-memory retry within the same Scheduler instance.
    This persists a DISPATCHED task with run_started_at set, reloads it via a fresh
    JsonStateStore (simulating a new process), runs it through the crash-recovery path
    (reconcile_task_recovery, which detects the dead pid and demotes DISPATCHED ->
    RETRY), and confirms run_started_at is preserved throughout -- then feeds the
    reloaded/reconciled task into a fresh Scheduler and confirms _expire_overrun_tasks
    still enforces the budget against the reloaded value."""
    from subsched.contract import bootstrap_task_files
    from subsched.recovery import ProcessRecord, reconcile_task_recovery, save_process_record

    state_path = tmp_path / "state.json"
    started = datetime(2026, 1, 1, tzinfo=UTC)
    task = (
        Task.from_issue(Issue(number=101, title="Task 101"))
        .transition(TaskState.DISPATCHED, current_agent="claude")
        .transition(TaskState.IN_PROGRESS, current_agent="claude")
    )
    from dataclasses import replace

    task = replace(task, run_started_at=started)
    JsonStateStore(state_path).save_tasks((task,))
    bootstrap_task_files(tmp_path, task)
    save_process_record(
        tmp_path,
        ProcessRecord(
            pid=999999,  # near-certainly-dead pid
            started_at=started.isoformat(),
            agent="claude",
            issue_number=101,
            worktree=str(tmp_path),
            attempt_nonce="nonce_137",
        ),
    )

    # Simulate a new process: fresh store instance loading the same file from disk.
    reloaded_store = JsonStateStore(state_path)
    reloaded_task = reloaded_store.load_tasks()[0]
    assert reloaded_task.run_started_at == started

    reconciled_task, _msg = reconcile_task_recovery(tmp_path, reloaded_task)
    assert reconciled_task.status is TaskState.RETRY
    assert reconciled_task.run_started_at == started
    reloaded_store.save_tasks((reconciled_task,))

    scheduler = Scheduler(
        store=reloaded_store,
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({}),
        worktree_root=tmp_path / "worktrees",
        max_task_runtime_seconds=3600.0,
    )
    far_future = started + timedelta(hours=2)
    scheduler.tick([], now=far_future)

    expired = scheduler.tasks[0]
    assert expired.status is TaskState.NEEDS_HUMAN
    assert expired.needs_human_reason is not None
    assert "max_task_runtime" in expired.needs_human_reason


def test_never_dispatched_task_is_untouched_by_the_budget(tmp_path: Path) -> None:
    """A task that was never dispatched (run_started_at is None) must not be expired --
    only tasks that have actually consumed runtime budget are subject to the limit."""
    scheduler = _scheduler(tmp_path, max_task_runtime_seconds=1.0)
    scheduler.discover([Issue(number=999, title="Never dispatched")])

    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    scheduler.tick([], now=far_future)

    assert scheduler.tasks[0].status is not TaskState.NEEDS_HUMAN
    assert scheduler.tasks[0].run_started_at is None
