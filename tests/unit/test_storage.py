import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from subsched.models import Capacity, CapacityState, Issue, Task, TaskState
from subsched.storage import JsonStateStore, StateCorruptionError


def test_state_round_trip_preserves_recovery_fields(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    task = Task.from_issue(Issue(number=7, title="seven")).with_worktree("/worktrees/7")
    task = task.transition(TaskState.DISPATCHED, current_agent="claude")

    store.save_tasks((task,))

    assert store.load_tasks() == (task,)


def test_corrupt_state_fails_closed_without_overwriting(tmp_path: Path) -> None:
    state_file = tmp_path / ".ai" / "scheduler.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("{broken", encoding="utf-8")

    with pytest.raises(StateCorruptionError):
        JsonStateStore(tmp_path).load_tasks()

    assert state_file.read_text(encoding="utf-8") == "{broken"


def test_unknown_schema_version_fails_closed(tmp_path: Path) -> None:
    state_file = tmp_path / ".ai" / "scheduler.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"schema_version": 999, "tasks": []}), encoding="utf-8")

    with pytest.raises(StateCorruptionError, match="schema"):
        JsonStateStore(tmp_path).load_tasks()


def test_capacity_cooldown_round_trip(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    capacity = Capacity(
        agent="claude",
        state=CapacityState.COOLDOWN_SESSION,
        reset_at=now + timedelta(hours=1),
        observed_at=now,
        source="structured_result",
        confidence="high",
    )
    store = JsonStateStore(tmp_path)

    store.save_state((), capacities=(capacity,))

    assert store.load_capacities() == (capacity,)


def test_state_store_rejects_symlinked_state_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".ai").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StateCorruptionError, match="symlink"):
        JsonStateStore(tmp_path).save_tasks(())


def test_scheduler_rejects_persisted_available_capacity(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    capacity = Capacity(
        agent="codex",
        state=CapacityState.AVAILABLE,
        observed_at=now,
        source="provider",
        confidence="high",
    )
    store = JsonStateStore(tmp_path)
    store.save_state((), capacities=(capacity,))

    from subsched.router import AgentConfig, Router
    from subsched.scheduler import Scheduler, ScriptedWorker

    with pytest.raises(ValueError, match="cooldown blocker"):
        Scheduler(
            store=store,
            router=Router((AgentConfig("codex", 90),)),
            worker=ScriptedWorker({}),
            worktree_root=tmp_path / "worktrees",
        )


def test_scheduler_lock_mutual_exclusion(tmp_path: Path) -> None:
    from subsched.storage import SchedulerLock, SchedulerLockError

    lock_file = tmp_path / ".ai" / "scheduler.lock"
    lock1 = SchedulerLock(lock_file)
    lock2 = SchedulerLock(lock_file)

    lock1.acquire()
    assert lock1._held is True

    with pytest.raises(SchedulerLockError, match="already running"):
        lock2.acquire()

    lock1.release()
    assert lock1._held is False

    # lock2 can now acquire
    lock2.acquire()
    assert lock2._held is True
    lock2.release()


def test_scheduler_lock_stale_lock_recovery(tmp_path: Path) -> None:
    from subsched.storage import LockRecord, SchedulerLock

    lock_file = tmp_path / ".ai" / "scheduler.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    # Stale record with non-existent dead PID 999999
    stale_record = LockRecord(
        pid=999999,
        process_start_time="2020-01-01T00:00:00Z",
        nonce="deadbeef",
        created_at="2020-01-01T00:00:00Z",
        hostname="old-host",
    )
    lock_file.write_text(json.dumps(stale_record.to_dict()), encoding="utf-8")

    lock = SchedulerLock(lock_file)
    lock.acquire()
    assert lock._held is True
    lock.release()


def test_state_store_cas_lost_update_detection(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.save_tasks(())
    rev = store.get_revision()

    # Save with matching expected_revision succeeds
    store.save_tasks((), expected_revision=rev)
    new_rev = store.get_revision()
    assert new_rev == rev + 1

    # Save with stale expected_revision raises StateCorruptionError
    with pytest.raises(StateCorruptionError, match="lost update"):
        store.save_tasks((), expected_revision=rev)
