from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.models import (
    AgentResult,
    AgentResultKind,
    Capacity,
    CapacityState,
    Issue,
    TaskState,
)
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore


def _available(agent: str, now: datetime) -> Capacity:
    return Capacity(
        agent=agent,
        state=CapacityState.AVAILABLE,
        observed_at=now,
        source="provider",
        confidence="high",
    )


def test_capacity_temporary_sets_rate_limited_temporary_without_agent_failure(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    store = JsonStateStore(tmp_path)
    router = Router((AgentConfig("claude", 100),))
    worker = ScriptedWorker(
        {(1, "claude"): (AgentResult(AgentResultKind.CAPACITY_TEMPORARY, output="overloaded"),)}
    )
    scheduler = Scheduler(
        store=store, router=router, worker=worker, worktree_root=tmp_path / "worktrees"
    )
    scheduler.discover((Issue(number=1, title="one"),))

    scheduler.tick((_available("claude", now),), now=now)

    # Cooldown must be recorded as RATE_LIMITED_TEMPORARY
    cooldown = scheduler._cooldowns.get("claude")
    assert cooldown is not None
    assert cooldown.state is CapacityState.RATE_LIMITED_TEMPORARY

    # Must NOT increment per_agent_failures
    task = scheduler.tasks[0]
    assert dict(task.per_agent_failures) == {}
    assert task.status is not TaskState.NEEDS_HUMAN
    assert task.capacity_events == 1


def test_capacity_temporary_repeated_occurrences_do_not_escalate_to_needs_human(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    store = JsonStateStore(tmp_path)
    router = Router((AgentConfig("claude", 100),))

    # 10 consecutive temporary capacity events (exceeds default max_agent_switches=6)
    results = tuple(
        AgentResult(AgentResultKind.CAPACITY_TEMPORARY, output="busy") for _ in range(10)
    )
    worker = ScriptedWorker({(1, "claude"): results})
    scheduler = Scheduler(
        store=store,
        router=router,
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        max_agent_switches=6,
    )
    scheduler.discover((Issue(number=1, title="one"),))

    curr_time = now
    for i in range(10):
        # Dispatch with available probe after cooldown reset
        scheduler.tick((_available("claude", curr_time),), now=curr_time)
        task = scheduler.tasks[0]
        assert task.status is not TaskState.NEEDS_HUMAN, f"Escalated on occurrence {i+1}"
        # Advance beyond the cooldown reset
        reset_at = scheduler._cooldowns["claude"].reset_at
        assert reset_at is not None
        curr_time = reset_at + timedelta(seconds=1)

    assert scheduler.tasks[0].capacity_events == 10
    assert dict(scheduler.tasks[0].per_agent_failures) == {}


def test_capacity_cooldown_priority_session_and_weekly_precede_temporary(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    store = JsonStateStore(tmp_path)
    router = Router((AgentConfig("claude", 100),))

    session_reset = now + timedelta(hours=2)
    worker = ScriptedWorker(
        {
            (1, "claude"): (
                AgentResult(AgentResultKind.CAPACITY_SESSION, reset_at=session_reset),
                AgentResult(AgentResultKind.CAPACITY_TEMPORARY),
            )
        }
    )
    scheduler = Scheduler(
        store=store, router=router, worker=worker, worktree_root=tmp_path / "worktrees"
    )
    scheduler.discover((Issue(number=1, title="one"),))

    # First event: session capacity
    scheduler.tick((_available("claude", now),), now=now)
    assert scheduler._cooldowns["claude"].state is CapacityState.COOLDOWN_SESSION

    # Even if another result reports temporary capacity, session cooldown must not be downgraded
    scheduler._handle_result(
        scheduler.tasks[0], "claude", AgentResult(AgentResultKind.CAPACITY_TEMPORARY), now=now
    )
    assert scheduler._cooldowns["claude"].state is CapacityState.COOLDOWN_SESSION
