from __future__ import annotations

import concurrent.futures
from datetime import UTC, datetime

import pytest

from subsched.lease import LeaseError, LeaseManager, TaskLease
from subsched.models import Task, TaskState


def test_lease_manager_invalid_concurrency() -> None:
    with pytest.raises(ValueError, match="max_concurrency must be positive"):
        LeaseManager(max_concurrency=0)


def test_lease_manager_acquire_and_release() -> None:
    now = datetime.now(UTC)
    manager = LeaseManager(max_concurrency=2)
    assert manager.active_count == 0

    lease1 = manager.acquire(101, "claude", now=now)
    assert lease1.issue_number == 101
    assert lease1.agent == "claude"
    assert manager.is_agent_busy("claude")
    assert manager.is_task_leased(101)
    assert manager.active_count == 1

    # Cannot acquire same issue again
    with pytest.raises(LeaseError, match="already leased"):
        manager.acquire(101, "codex", now=now)

    # Cannot acquire same agent again
    with pytest.raises(LeaseError, match="already busy"):
        manager.acquire(102, "claude", now=now)

    # Can acquire second task on different agent (concurrency=2)
    lease2 = manager.acquire(102, "codex", now=now)
    assert lease2.issue_number == 102
    assert lease2.agent == "codex"
    assert manager.active_count == 2

    # Concurrency limit reached
    with pytest.raises(LeaseError, match="concurrency limit"):
        manager.acquire(103, "other", now=now)

    # Releasing non-existent issue is a no-op
    manager.release(999)

    # Releasing with mismatched nonce is ignored
    manager.release(101, nonce="wrong-nonce")
    assert manager.is_task_leased(101)

    # Release lease1 with matching nonce
    manager.release(101, nonce=lease1.nonce)
    assert not manager.is_agent_busy("claude")
    assert not manager.is_task_leased(101)
    assert manager.active_count == 1

    # Now claude is free to acquire
    lease3 = manager.acquire(103, "claude", now=now)
    assert lease3.issue_number == 103


def test_lease_manager_reconcile() -> None:
    now = datetime.now(UTC)
    manager = LeaseManager(max_concurrency=2)
    manager.acquire(101, "claude", now=now)
    manager.acquire(102, "codex", now=now)

    # Reconcile with tasks where 101 is COMPLETE and 102 is IN_PROGRESS
    t1 = Task(
        task_id="github-101",
        issue_number=101,
        title="Task 101",
        labels=(),
        status=TaskState.COMPLETE,
    )
    t2 = Task(
        task_id="github-102",
        issue_number=102,
        title="Task 102",
        labels=(),
        status=TaskState.IN_PROGRESS,
        current_agent="codex",
    )

    manager.reconcile([t1, t2])
    assert not manager.is_task_leased(101)
    assert not manager.is_agent_busy("claude")
    assert manager.is_task_leased(102)
    assert manager.is_agent_busy("codex")


def test_lease_manager_thread_safety_race() -> None:
    manager = LeaseManager(max_concurrency=1)
    successes: list[TaskLease] = []
    failures: list[Exception] = []

    def try_acquire(issue_num: int, agent_name: str) -> None:
        try:
            acquired = manager.acquire(issue_num, agent_name)
            successes.append(acquired)
        except Exception as err:
            failures.append(err)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(try_acquire, 101, f"agent-{i}") for i in range(8)
        ]
        concurrent.futures.wait(futures)

    assert len(successes) == 1
    assert len(failures) == 7
