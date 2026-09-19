from __future__ import annotations

import multiprocessing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
from subsched.storage import JsonStateStore, SchedulerLockError, StateCorruptionError


def _scheduler(tmp_path: Path, worker: ScriptedWorker | None = None) -> Scheduler:
    return Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100), AgentConfig("codex", 90))),
        worker=worker or ScriptedWorker({}),
        worktree_root=tmp_path / "worktrees",
    )


def _discover_in_separate_process(repository: str, issue_number: int) -> None:
    _scheduler(Path(repository)).discover((Issue(number=issue_number, title="external"),))


def _available_capacity() -> Capacity:
    return Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        observed_at=datetime(2026, 9, 19, tzinfo=UTC),
        source="provider",
        confidence="high",
    )


def test_persist_is_blocked_while_another_process_holds_the_scheduler_lock(
    tmp_path: Path,
) -> None:
    """Scheduler._persist() must serialize with CLI writers (pause/resume/cancel) via the
    same process-level lock, so a concurrent CLI command cannot race a Scheduler tick and
    silently lose either write."""
    store = JsonStateStore(tmp_path)
    scheduler = _scheduler(tmp_path)

    external_lock = store.lock()
    external_lock.acquire()
    try:
        try:
            scheduler.discover((Issue(number=101, title="one"),))
        except SchedulerLockError:
            pass
        else:
            raise AssertionError(
                "expected SchedulerLockError while the lock is held by another process"
            )
    finally:
        external_lock.release()

    # The in-memory queue mutation from the failed discover() call must not have leaked
    # into persisted state.
    assert store.load_tasks() == ()


def test_persist_succeeds_and_releases_the_lock_for_subsequent_writers(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    scheduler = _scheduler(tmp_path)

    scheduler.discover((Issue(number=101, title="one"),))
    assert [task.issue_number for task in store.load_tasks()] == [101]

    # A subsequent external writer (e.g. `subsched cancel`) must be able to take the lock
    # immediately afterwards; no deadlock or leaked lock file.
    with store.lock():
        store.save_tasks(store.load_tasks(), paused=True)
    assert store.is_paused() is True


def test_stale_scheduler_preserves_external_cancel_and_does_not_dispatch(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.save_tasks((Task.from_issue(Issue(number=101, title="one")),))
    worker = ScriptedWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = _scheduler(tmp_path, worker)

    with store.lock():
        task = store.load_tasks()[0]
        store.save_tasks((task.transition(TaskState.CANCELLED),))

    with pytest.raises(StateCorruptionError, match="lost update"):
        scheduler.tick((_available_capacity(),))

    assert worker.dispatches == []
    assert store.load_tasks()[0].status is TaskState.CANCELLED


def test_stale_scheduler_preserves_external_queue_addition(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.save_tasks((Task.from_issue(Issue(number=101, title="one")),))
    stale_scheduler = _scheduler(tmp_path)
    external_scheduler = _scheduler(tmp_path)

    external_scheduler.discover((Issue(number=102, title="two"),))

    with pytest.raises(StateCorruptionError, match="lost update"):
        stale_scheduler.refresh_capacities(())

    assert [task.issue_number for task in store.load_tasks()] == [101, 102]


def test_stale_scheduler_preserves_external_needs_human_resolution(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    cooldown = replace(_available_capacity(), state=CapacityState.COOLDOWN_SESSION)
    needs_human = replace(
        Task.from_issue(Issue(number=101, title="one")),
        status=TaskState.NEEDS_HUMAN,
        needs_human_reason="operator action required",
    )
    store.save_state((needs_human,), capacities=(cooldown,))
    stale_scheduler = _scheduler(tmp_path)

    with store.lock():
        resolved = store.load_tasks()[0].transition(TaskState.READY)
        store.save_tasks((resolved,), paused=True)
    external_revision = store.get_revision()

    with pytest.raises(StateCorruptionError, match="lost update"):
        stale_scheduler.refresh_capacities(())

    assert store.load_tasks()[0].status is TaskState.READY
    assert store.is_paused() is True
    assert store.load_capacities() == (cooldown,)
    assert store.get_revision() == external_revision


def test_scheduler_merges_external_pause_when_tasks_and_capacities_are_unchanged(
    tmp_path: Path,
) -> None:
    store = JsonStateStore(tmp_path)
    store.save_tasks((Task.from_issue(Issue(number=101, title="one")),))
    scheduler = _scheduler(tmp_path)

    store.set_paused(True)
    scheduler.refresh_capacities(())

    assert store.is_paused() is True
    assert store.load_tasks() == scheduler.tasks


def test_stale_scheduler_preserves_separate_process_scheduler_update(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.save_tasks((Task.from_issue(Issue(number=101, title="one")),))
    stale_scheduler = _scheduler(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_discover_in_separate_process,
        args=(str(tmp_path), 102),
    )

    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.kill()
        process.join()
        raise AssertionError("separate-process Scheduler writer did not exit")
    assert process.exitcode == 0

    with pytest.raises(StateCorruptionError, match="lost update"):
        stale_scheduler.refresh_capacities(())

    assert [task.issue_number for task in store.load_tasks()] == [101, 102]
