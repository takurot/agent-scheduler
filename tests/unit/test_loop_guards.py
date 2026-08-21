from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.models import AgentResult, AgentResultKind, Capacity, CapacityState, Issue
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


def test_durable_loop_guards_separate_counters(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    router = Router([
        AgentConfig(name="claude", priority=100, enabled=True),
        AgentConfig(name="codex", priority=90, enabled=True),
    ])
    now = datetime(2026, 8, 22, 0, 0, tzinfo=UTC)
    reset = now + timedelta(hours=1)

    scripted = ScriptedWorker({
        (101, "claude"): (
            AgentResult(AgentResultKind.CAPACITY_SESSION, reset_at=reset, output="session"),
        ),
        (101, "codex"): (AgentResult(AgentResultKind.FAILURE, output="failed"),),
    })
    scheduler = Scheduler(
        store=store,
        router=router,
        worker=scripted,
        worktree_root=tmp_path / "worktrees",
    )
    scheduler.discover([Issue(number=101, title="Loop guard test")])

    capacities = (available("claude", now), available("codex", now))

    # Tick 1: claude capacity failure
    assert scheduler.tick(capacities, now=now) is True
    task = scheduler.tasks[0]
    assert task.capacity_events == 1
    assert task.attempt == 0
    assert task.actual_agent_switches == 0

    # Tick 2: codex runs, actual switch happens, failure increments per_agent_failures and attempt
    assert scheduler.tick(capacities, now=now) is True
    task = scheduler.tasks[0]
    assert task.capacity_events == 1
    assert task.actual_agent_switches == 1
    assert task.attempt == 1
    assert dict(task.per_agent_failures) == {"codex": 1}

    # Verify state persistence and restoration
    restored_scheduler = Scheduler(
        store=store,
        router=router,
        worker=scripted,
        worktree_root=tmp_path / "worktrees",
    )
    restored_task = restored_scheduler.tasks[0]
    assert restored_task.capacity_events == 1
    assert restored_task.actual_agent_switches == 1
    assert restored_task.attempt == 1
    assert dict(restored_task.per_agent_failures) == {"codex": 1}
