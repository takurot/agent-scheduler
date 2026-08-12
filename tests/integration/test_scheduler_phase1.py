from datetime import UTC, datetime, timedelta
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
from subsched.storage import JsonStateStore


def available(agent: str, now: datetime) -> Capacity:
    return Capacity(
        agent=agent,
        state=CapacityState.AVAILABLE,
        used_percentage=10,
        reset_at=now + timedelta(hours=5),
        observed_at=now,
        source="provider",
        confidence="high",
    )


def test_phase1_queue_survives_capacity_failover_and_preserves_worktree(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    reset = now + timedelta(hours=1)
    worker = ScriptedWorker(
        {
            (101, "claude"): (AgentResult(AgentResultKind.PASS),),
            (102, "claude"): (AgentResult(AgentResultKind.CAPACITY_SESSION, reset_at=reset),),
            (102, "codex"): (AgentResult(AgentResultKind.PASS),),
            (103, "codex"): (AgentResult(AgentResultKind.CAPACITY_WEEKLY, reset_at=reset),),
            (103, "claude"): (AgentResult(AgentResultKind.PASS),),
        }
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100), AgentConfig("codex", 90))),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
    )
    scheduler.discover(tuple(Issue(number=n, title=str(n)) for n in (101, 102, 103)))

    capacities = (available("claude", now), available("codex", now))
    scheduler.run_until_waiting(capacities, now=now)
    assert scheduler.is_waiting_for_capacity
    refreshed = (available("claude", reset), available("codex", reset))
    scheduler.run_until_waiting(refreshed, now=reset)

    assert [task.status.value for task in scheduler.tasks] == ["COMPLETE"] * 3
    assert worker.dispatches == [
        (101, "claude"),
        (102, "claude"),
        (102, "codex"),
        (103, "codex"),
        (103, "claude"),
    ]
    paths_102 = [path for issue, _, path in worker.worktrees if issue == 102]
    paths_103 = [path for issue, _, path in worker.worktrees if issue == 103]
    assert len(set(paths_102)) == 1
    assert len(set(paths_103)) == 1
    assert JsonStateStore(tmp_path).load_tasks() == scheduler.tasks


def test_restart_preserves_cooldown_and_does_not_redispatch_before_probe(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    reset = now + timedelta(hours=1)
    store = JsonStateStore(tmp_path)
    router = Router((AgentConfig("claude", 100),))
    first_worker = ScriptedWorker(
        {(1, "claude"): (AgentResult(AgentResultKind.CAPACITY_SESSION, reset_at=reset),)}
    )
    first = Scheduler(
        store=store, router=router, worker=first_worker, worktree_root=tmp_path / "worktrees"
    )
    first.discover((Issue(number=1, title="one"),))
    snapshot = (available("claude", now),)
    first.run_until_waiting(snapshot, now=now)

    second_worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    restarted = Scheduler(
        store=store, router=router, worker=second_worker, worktree_root=tmp_path / "worktrees"
    )
    restarted.run_until_waiting(snapshot, now=now + timedelta(minutes=5))

    assert restarted.is_waiting_for_capacity
    assert second_worker.dispatches == []


def test_paused_scheduler_does_not_dispatch(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    store = JsonStateStore(tmp_path)
    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
    )
    scheduler.discover((Issue(number=1, title="one"),))
    store.set_paused(True)

    scheduler.run_until_waiting((available("claude", now),), now=now)

    assert worker.dispatches == []


def test_completed_dependency_is_released(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    worker = ScriptedWorker(
        {
            (1, "claude"): (AgentResult(AgentResultKind.PASS),),
            (2, "claude"): (AgentResult(AgentResultKind.PASS),),
        }
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
    )
    scheduler.discover(
        (Issue(number=1, title="one"), Issue(number=2, title="two", body="Blocked-By: #1"))
    )

    scheduler.run_until_waiting((available("claude", now),), now=now)

    assert [task.status for task in scheduler.tasks] == [TaskState.COMPLETE, TaskState.COMPLETE]


def test_worker_exceptions_stop_at_human_intervention_limit(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    worker = ScriptedWorker({})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        max_agent_failures=2,
    )
    scheduler.discover((Issue(number=1, title="one"),))

    scheduler.run_until_waiting((available("claude", now),), now=now)

    assert scheduler.tasks[0].status is TaskState.NEEDS_HUMAN
    assert scheduler.tasks[0].attempt == 2


def test_capacity_switches_stop_at_human_intervention_limit(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    reset = now + timedelta(hours=1)
    worker = ScriptedWorker(
        {
            (1, "claude"): (AgentResult(AgentResultKind.CAPACITY_SESSION, reset_at=reset),),
            (1, "codex"): (AgentResult(AgentResultKind.CAPACITY_WEEKLY, reset_at=reset),),
        }
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100), AgentConfig("codex", 90))),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        max_agent_switches=2,
    )
    scheduler.discover((Issue(number=1, title="one"),))

    scheduler.run_until_waiting((available("claude", now), available("codex", now)), now=now)

    assert scheduler.tasks[0].status is TaskState.NEEDS_HUMAN
    assert scheduler.tasks[0].agent_switches == 2


def test_local_estimate_does_not_release_provider_cooldown(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    reset = now + timedelta(hours=1)
    store = JsonStateStore(tmp_path)
    worker = ScriptedWorker(
        {(1, "claude"): (AgentResult(AgentResultKind.CAPACITY_SESSION, reset_at=reset),)}
    )
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
    )
    scheduler.discover((Issue(number=1, title="one"),))
    scheduler.run_until_waiting((available("claude", now),), now=now)
    local = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=reset,
        source="local_estimate",
        confidence="low",
    )
    next_worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    restarted = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=next_worker,
        worktree_root=tmp_path / "worktrees",
    )

    restarted.run_until_waiting((local,), now=reset)

    assert next_worker.dispatches == []


def test_worktree_symlink_is_rejected(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    root = tmp_path / "worktrees"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "issue-1").symlink_to(outside, target_is_directory=True)
    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=root,
    )
    scheduler.discover((Issue(number=1, title="one"),))

    with pytest.raises(ValueError, match="worktree"):
        scheduler.run_until_waiting((available("claude", now),), now=now)

    assert worker.dispatches == []


def test_future_probe_does_not_release_cooldown_before_reset(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    reset = now + timedelta(hours=1)
    store = JsonStateStore(tmp_path)
    first_worker = ScriptedWorker(
        {(1, "claude"): (AgentResult(AgentResultKind.CAPACITY_SESSION, reset_at=reset),)}
    )
    first = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=first_worker,
        worktree_root=tmp_path / "worktrees",
    )
    first.discover((Issue(number=1, title="one"),))
    first.run_until_waiting((available("claude", now),), now=now)
    future_probe = available("claude", reset + timedelta(seconds=1))
    next_worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    restarted = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=next_worker,
        worktree_root=tmp_path / "worktrees",
    )

    restarted.run_until_waiting((future_probe,), now=now)

    assert next_worker.dispatches == []


def test_invalid_persisted_worktree_is_rejected_before_directory_creation(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    outside = tmp_path / "outside" / "nested"
    store = JsonStateStore(tmp_path)
    task = Task.from_issue(Issue(number=1, title="one")).with_worktree(str(outside))
    store.save_tasks((task,))
    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
    )

    with pytest.raises(ValueError, match="worktree"):
        scheduler.run_until_waiting((available("claude", now),), now=now)

    assert outside.exists() is False
    assert worker.dispatches == []
