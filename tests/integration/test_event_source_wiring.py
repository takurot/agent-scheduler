from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.events import Event, EventType, FakeClock, FakeEventSource
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


def test_event_source_full_lifecycle_integration(tmp_path: Path) -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    worker = ScriptedWorker({
        (1, "claude"): (AgentResult(AgentResultKind.PASS),),
        (2, "claude"): (AgentResult(AgentResultKind.PASS),),
    })

    store = JsonStateStore(tmp_path)
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )
    scheduler.discover((
        Issue(number=1, title="one"),
        Issue(number=2, title="two", body="Blocked-By: #1"),
    ))

    # Initial tick: no capacity supplied, task 1 waits for capacity
    assert scheduler.tick() is False
    assert scheduler.queue.get(1).status is TaskState.WAITING_CAPACITY
    assert scheduler.queue.get(2).status is TaskState.WAITING_DEPENDENCY

    # 1. External sensor emits CAPACITY_PROBE
    fresh_cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=t0,
        source="provider",
        confidence="high",
    )
    event_source.emit(
        Event(
            event_type=EventType.CAPACITY_PROBE,
            timestamp=t0,
            payload={"capacity": fresh_cap},
        )
    )

    # Scheduler ticks, processes probe, wakes task 1, and dispatches it
    assert scheduler.tick() is True
    assert scheduler.queue.get(1).status is TaskState.COMPLETE
    # Dependency on task 1 is resolved, so task 2 is released to READY and dispatched!
    assert scheduler.tick((fresh_cap,)) is True
    assert scheduler.queue.get(2).status is TaskState.COMPLETE
    assert worker.dispatches == [(1, "claude"), (2, "claude")]


def test_event_source_task_completed_and_capacity_reset_integration(tmp_path: Path) -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    worker = ScriptedWorker({
        (2, "claude"): (AgentResult(AgentResultKind.PASS),),
    })

    store = JsonStateStore(tmp_path)
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )
    scheduler.discover((
        Issue(number=1, title="one"),
        Issue(number=2, title="two", body="Blocked-By: #1"),
    ))

    # Place task 1 in READY_FOR_REVIEW
    t1 = scheduler.queue.get(1)
    t1 = t1.transition(TaskState.DISPATCHED, current_agent="claude", now=t0)
    t1 = t1.transition(TaskState.IN_PROGRESS, current_agent="claude", now=t0)
    t1 = t1.transition(TaskState.VERIFYING, current_agent="claude", now=t0)
    t1 = t1.transition(TaskState.PR_READY, current_agent="claude", now=t0)
    t1 = t1.transition(TaskState.READY_FOR_REVIEW, current_agent="claude", now=t0)
    scheduler.queue = scheduler.queue.replace(t1)
    scheduler._persist()

    # Agent "claude" is in cooldown
    reset_at = t0 + timedelta(hours=2)
    cooldown_cap = Capacity(
        agent="claude",
        state=CapacityState.COOLDOWN_SESSION,
        observed_at=t0,
        source="provider",
        confidence="high",
        reset_at=reset_at,
    )
    scheduler._cooldowns["claude"] = cooldown_cap
    scheduler._persist()

    # 1. TASK_COMPLETED event arrives for task 1
    event_source.emit(
        Event(
            event_type=EventType.TASK_COMPLETED,
            timestamp=t0,
            payload={"issue_number": 1},
        )
    )
    # Tick processes TASK_COMPLETED, advancing task 1 to COMPLETE and task 2 to READY.
    # But because "claude" is in cooldown, task 2 goes to WAITING_CAPACITY.
    assert scheduler.tick() is False
    assert scheduler.queue.get(1).status is TaskState.COMPLETE
    assert scheduler.queue.get(2).status is TaskState.WAITING_CAPACITY

    # 2. Advance time to reset_at and emit CAPACITY_RESET without fresh probe
    clock.set_time(reset_at)
    event_source.emit(
        Event(
            event_type=EventType.CAPACITY_RESET,
            timestamp=reset_at,
            payload={"agent": "claude"},
        )
    )
    # Cooldown is NOT cleared without fresh probe; task 2 remains WAITING_CAPACITY
    assert scheduler.tick() is False
    assert "claude" in scheduler._cooldowns
    assert scheduler.queue.get(2).status is TaskState.WAITING_CAPACITY

    # 3. CAPACITY_RESET arrives WITH fresh high-confidence provider probe
    fresh_cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=reset_at,
        source="provider",
        confidence="high",
    )
    event_source.emit(
        Event(
            event_type=EventType.CAPACITY_RESET,
            timestamp=reset_at,
            payload={"agent": "claude", "capacity": fresh_cap},
        )
    )
    # Tick clears cooldown, wakes task 2, and dispatches it
    assert scheduler.tick() is True
    assert "claude" not in scheduler._cooldowns
    assert scheduler.queue.get(2).status is TaskState.COMPLETE
    assert worker.dispatches == [(2, "claude")]
