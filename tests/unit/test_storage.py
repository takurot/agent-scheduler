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
