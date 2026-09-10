from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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


def test_fake_clock_advance_and_set() -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    assert clock.now() == t0

    clock.advance(timedelta(hours=1))
    assert clock.now() == t0 + timedelta(hours=1)

    t1 = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)
    clock.set_time(t1)
    assert clock.now() == t1


def test_fake_event_source_polling() -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    e1 = Event(event_type=EventType.PAUSE, timestamp=t0)
    e2 = Event(event_type=EventType.RESUME, timestamp=t0 + timedelta(minutes=10))

    source = FakeEventSource((e1, e2))
    assert source.poll(t0 - timedelta(seconds=1)) == ()
    assert source.poll(t0) == (e1,)
    assert source.poll(t0 + timedelta(minutes=5)) == ()
    assert source.poll(t0 + timedelta(minutes=10)) == (e2,)
    assert source.poll(t0 + timedelta(minutes=15)) == ()


def test_scheduler_step_based_tick_with_fake_clock(tmp_path: Path) -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
    )
    scheduler.discover((Issue(number=1, title="one"),))

    cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=t0,
        source="provider",
        confidence="high",
    )

    # 1st tick runs and completes task 1
    action_taken = scheduler.tick((cap,))
    assert action_taken is True
    assert scheduler.queue.get(1).status is TaskState.COMPLETE

    # 2nd tick finds no ready tasks and returns False
    action_taken = scheduler.tick((cap,))
    assert action_taken is False


def test_scheduler_event_source_pause_resume(tmp_path: Path) -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    pause_event = Event(event_type=EventType.PAUSE, timestamp=t0)
    resume_event = Event(event_type=EventType.RESUME, timestamp=t0 + timedelta(minutes=5))
    event_source = FakeEventSource((pause_event, resume_event))

    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )
    scheduler.discover((Issue(number=1, title="one"),))

    cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=t0,
        source="provider",
        confidence="high",
    )

    # Tick at t0 processes PAUSE event and returns False without dispatching
    assert scheduler.tick((cap,)) is False
    assert scheduler.store.is_paused() is True
    assert worker.dispatches == []

    # Advance clock to resume time
    clock.advance(timedelta(minutes=5))
    # Tick now processes RESUME and executes task 1
    assert scheduler.tick((cap,)) is True
    assert scheduler.store.is_paused() is False
    assert worker.dispatches == [(1, "claude")]


def test_scheduler_event_source_capacity_probe_releases_waiting_tasks(tmp_path: Path) -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )
    scheduler.discover((Issue(number=1, title="one"),))

    # Tick without capacity puts task into WAITING_CAPACITY
    assert scheduler.tick() is False
    assert scheduler.queue.get(1).status is TaskState.WAITING_CAPACITY
    assert scheduler.is_waiting_for_capacity is True

    # Emit CAPACITY_PROBE with fresh available capacity
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

    # Next tick processes CAPACITY_PROBE, wakes waiting task, and dispatches it
    assert scheduler.tick() is True
    assert scheduler.queue.get(1).status is TaskState.COMPLETE
    assert scheduler.is_waiting_for_capacity is False
    assert worker.dispatches == [(1, "claude")]


def test_scheduler_event_source_capacity_probe_clears_cooldown(tmp_path: Path) -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    reset_at = t0 + timedelta(hours=1)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    store = JsonStateStore(tmp_path)
    cooldown_cap = Capacity(
        agent="claude",
        state=CapacityState.COOLDOWN_SESSION,
        observed_at=t0,
        source="provider",
        confidence="high",
        reset_at=reset_at,
    )
    store.save_state(tasks=(), capacities=(cooldown_cap,))

    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )
    scheduler.discover((Issue(number=1, title="one"),))

    # Before reset_at: task waits for capacity
    assert scheduler.tick() is False
    assert scheduler.queue.get(1).status is TaskState.WAITING_CAPACITY

    # Advance clock past reset_at
    probe_time = t0 + timedelta(hours=1, minutes=1)
    clock.set_time(probe_time)

    # CAPACITY_PROBE with fresh high-confidence provider probe
    fresh_cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=probe_time,
        source="provider",
        confidence="high",
    )
    event_source.emit(
        Event(
            event_type=EventType.CAPACITY_PROBE,
            timestamp=probe_time,
            payload={"capacities": [fresh_cap]},
        )
    )

    # Tick clears cooldown and dispatches task
    assert scheduler.tick() is True
    assert scheduler.queue.get(1).status is TaskState.COMPLETE
    assert "claude" not in scheduler._cooldowns


def test_scheduler_event_source_capacity_probe_retains_cooldown_with_low_confidence(
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    reset_at = t0 + timedelta(hours=1)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    store = JsonStateStore(tmp_path)
    cooldown_cap = Capacity(
        agent="claude",
        state=CapacityState.COOLDOWN_SESSION,
        observed_at=t0,
        source="provider",
        confidence="high",
        reset_at=reset_at,
    )
    store.save_state(tasks=(), capacities=(cooldown_cap,))

    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )
    scheduler.discover((Issue(number=1, title="one"),))

    probe_time = t0 + timedelta(hours=1, minutes=1)
    clock.set_time(probe_time)

    # Probe has source="policy" and confidence="low" (synthetic / unverified)
    low_conf_cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=probe_time,
        source="policy",
        confidence="low",
    )
    event_source.emit(
        Event(
            event_type=EventType.CAPACITY_PROBE,
            timestamp=probe_time,
            payload={"capacity": low_conf_cap},
        )
    )

    # Cooldown must be retained; task remains WAITING_CAPACITY
    assert scheduler.tick() is False
    assert scheduler.queue.get(1).status is TaskState.WAITING_CAPACITY
    assert "claude" in scheduler._cooldowns
    assert worker.dispatches == []


def test_scheduler_event_source_capacity_probe_applies_cooldown(tmp_path: Path) -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )
    scheduler.discover((Issue(number=1, title="one"),))

    # Emit CAPACITY_PROBE with COOLDOWN_SESSION
    cooldown_cap = Capacity(
        agent="claude",
        state=CapacityState.COOLDOWN_SESSION,
        observed_at=t0,
        source="provider",
        confidence="high",
        reset_at=t0 + timedelta(hours=1),
    )
    event_source.emit(
        Event(
            event_type=EventType.CAPACITY_PROBE,
            timestamp=t0,
            payload={"capacity": cooldown_cap},
        )
    )

    assert scheduler.tick() is False
    assert "claude" in scheduler._cooldowns
    assert scheduler.queue.get(1).status is TaskState.WAITING_CAPACITY


def test_scheduler_event_source_capacity_reset_without_fresh_probe_retains_cooldown(
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    reset_at = t0 + timedelta(hours=1)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    store = JsonStateStore(tmp_path)
    cooldown_cap = Capacity(
        agent="claude",
        state=CapacityState.COOLDOWN_SESSION,
        observed_at=t0,
        source="provider",
        confidence="high",
        reset_at=reset_at,
    )
    store.save_state(tasks=(), capacities=(cooldown_cap,))

    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )
    scheduler.discover((Issue(number=1, title="one"),))
    scheduler.tick()
    scheduler.tick()
    scheduler.tick()
    assert scheduler._backoff_step >= 2

    # Clock reaches reset time
    clock.set_time(reset_at)

    # CAPACITY_RESET arrives without a fresh probe (only specifies agent)
    event_source.emit(
        Event(
            event_type=EventType.CAPACITY_RESET,
            timestamp=reset_at,
            payload={"agent": "claude"},
        )
    )

    # Fresh probe contract: CAPACITY_RESET without fresh probe must NOT clear cooldown
    assert scheduler.tick() is False
    assert "claude" in scheduler._cooldowns
    assert scheduler.queue.get(1).status is TaskState.WAITING_CAPACITY
    assert worker.dispatches == []
    # Backoff step should be reset to 0 on reset event (and stepped to 1 on this empty tick)
    assert scheduler._backoff_step == 1


def test_scheduler_event_source_capacity_reset_with_fresh_probe_clears_cooldown(
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    reset_at = t0 + timedelta(hours=1)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    store = JsonStateStore(tmp_path)
    cooldown_cap = Capacity(
        agent="claude",
        state=CapacityState.COOLDOWN_SESSION,
        observed_at=t0,
        source="provider",
        confidence="high",
        reset_at=reset_at,
    )
    store.save_state(tasks=(), capacities=(cooldown_cap,))

    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )
    scheduler.discover((Issue(number=1, title="one"),))
    scheduler.tick()
    assert scheduler.queue.get(1).status is TaskState.WAITING_CAPACITY

    clock.set_time(reset_at)
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

    # With fresh high-confidence provider probe, cooldown is cleared and task dispatches
    assert scheduler.tick() is True
    assert "claude" not in scheduler._cooldowns
    assert scheduler.queue.get(1).status is TaskState.COMPLETE
    assert worker.dispatches == [(1, "claude")]


def test_scheduler_event_source_task_completed_advances_and_releases_dependencies(
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    worker = ScriptedWorker({
        (2, "claude"): (AgentResult(AgentResultKind.PASS),),
    })
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
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

    # Manually place task 1 in READY_FOR_REVIEW (as if verified, rebased, PR opened)
    task1 = scheduler.queue.get(1)
    task1 = task1.transition(TaskState.DISPATCHED, current_agent="claude", now=t0)
    task1 = task1.transition(TaskState.IN_PROGRESS, current_agent="claude", now=t0)
    task1 = task1.transition(TaskState.VERIFYING, current_agent="claude", now=t0)
    task1 = task1.transition(TaskState.PR_READY, current_agent="claude", now=t0)
    task1 = task1.transition(TaskState.READY_FOR_REVIEW, current_agent="claude", now=t0)
    scheduler.queue = scheduler.queue.replace(task1)
    scheduler._persist()

    # Task 2 is waiting on task 1
    assert scheduler.queue.get(2).status is TaskState.WAITING_DEPENDENCY

    cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=t0,
        source="provider",
        confidence="high",
    )

    # Emit TASK_COMPLETED event for task 1
    event_source.emit(
        Event(
            event_type=EventType.TASK_COMPLETED,
            timestamp=t0,
            payload={"issue_number": 1},
        )
    )

    # Tick completes task 1, releases task 2 to READY, and dispatches task 2
    assert scheduler.tick((cap,)) is True
    assert scheduler.queue.get(1).status is TaskState.COMPLETE
    assert scheduler.queue.get(2).status is TaskState.COMPLETE
    assert worker.dispatches == [(2, "claude")]


def test_scheduler_event_source_task_completed_rejects_illegal_transition(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    worker = ScriptedWorker({})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )
    scheduler.discover((Issue(number=1, title="one"),))
    # Task 1 is in READY state

    # Emit TASK_COMPLETED for task 1 (which is READY, not READY_FOR_REVIEW)
    event_source.emit(
        Event(
            event_type=EventType.TASK_COMPLETED,
            timestamp=t0,
            payload={"issue_number": 1},
        )
    )

    with caplog.at_level(logging.WARNING):
        scheduler.tick()

    # Task 1 must NOT transition to COMPLETE; stays READY (or WAITING_CAPACITY)
    assert scheduler.queue.get(1).status in (TaskState.READY, TaskState.WAITING_CAPACITY)
    assert any("TASK_COMPLETED" in record.message for record in caplog.records)


def test_scheduler_event_source_unsupported_event_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    worker = ScriptedWorker({})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )

    # Emit an unknown / unsupported event type
    fake_unknown_event = Event(
        event_type="UNKNOWN_FUTURE_EVENT",  # type: ignore
        timestamp=t0,
        payload={"foo": "bar"},
    )
    event_source.emit(fake_unknown_event)

    with caplog.at_level(logging.WARNING):
        scheduler.tick()

    assert any(
        "unsupported" in record.message.lower() or "unhandled" in record.message.lower()
        for record in caplog.records
    )


def test_scheduler_event_source_capacity_probe_invalid_payload_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    worker = ScriptedWorker({})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )

    # Empty payload
    event_source.emit(
        Event(
            event_type=EventType.CAPACITY_PROBE,
            timestamp=t0,
            payload={},
        )
    )

    with caplog.at_level(logging.WARNING):
        scheduler.tick()

    assert any("missing capacity payload" in record.message for record in caplog.records)


def test_scheduler_event_source_capacity_reset_stale_probe_retains_cooldown(
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    reset_at = t0 + timedelta(hours=1)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    store = JsonStateStore(tmp_path)
    cooldown_cap = Capacity(
        agent="claude",
        state=CapacityState.COOLDOWN_SESSION,
        observed_at=t0,
        source="provider",
        confidence="high",
        reset_at=reset_at,
    )
    store.save_state(tasks=(), capacities=(cooldown_cap,))

    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )
    scheduler.discover((Issue(number=1, title="one"),))
    scheduler.tick()
    assert scheduler.queue.get(1).status is TaskState.WAITING_CAPACITY

    clock.set_time(reset_at)
    # Stale probe: observed_at is t0, which is BEFORE reset_at
    stale_cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=t0,
        source="provider",
        confidence="high",
    )
    event_source.emit(
        Event(
            event_type=EventType.CAPACITY_RESET,
            timestamp=reset_at,
            payload={"agent": "claude", "capacity": stale_cap},
        )
    )

    # Stale probe does not clear cooldown
    assert scheduler.tick() is False
    assert "claude" in scheduler._cooldowns
    assert scheduler.queue.get(1).status is TaskState.WAITING_CAPACITY


def test_scheduler_event_source_task_completed_idempotent_and_missing_payload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )
    scheduler.discover((Issue(number=1, title="one"),))

    cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=t0,
        source="provider",
        confidence="high",
    )
    # 1. Complete task 1 normally
    assert scheduler.tick((cap,)) is True
    assert scheduler.queue.get(1).status is TaskState.COMPLETE

    # 2. TASK_COMPLETED arrives for already COMPLETE task 1 (idempotent no-op)
    event_source.emit(
        Event(
            event_type=EventType.TASK_COMPLETED,
            timestamp=t0,
            payload={"issue_number": 1},
        )
    )
    assert scheduler.tick((cap,)) is False
    assert scheduler.queue.get(1).status is TaskState.COMPLETE

    # 3. TASK_COMPLETED arrives with missing issue_number and task_id
    event_source.emit(
        Event(
            event_type=EventType.TASK_COMPLETED,
            timestamp=t0,
            payload={},
        )
    )
    with caplog.at_level(logging.WARNING):
        scheduler.tick((cap,))

    assert any(
        "missing issue_number and task_id" in record.message for record in caplog.records
    )


def test_scheduler_event_source_dict_payload(tmp_path: Path) -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )
    scheduler.discover((Issue(number=1, title="one"),))

    assert scheduler.tick() is False
    assert scheduler.queue.get(1).status is TaskState.WAITING_CAPACITY

    # Pass capacity as raw dict (simulating JSON payload from webhook / wire)
    event_source.emit(
        Event(
            event_type=EventType.CAPACITY_PROBE,
            timestamp=t0,
            payload={
                "capacity": {
                    "agent": "claude",
                    "state": "AVAILABLE",
                    "observed_at": t0.isoformat(),
                    "source": "provider",
                    "confidence": "high",
                }
            },
        )
    )

    assert scheduler.tick() is True
    assert scheduler.queue.get(1).status is TaskState.COMPLETE
    assert worker.dispatches == [(1, "claude")]


def test_scheduler_event_source_dict_payload_with_reset_at_and_invalid_entry(
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    reset_at = t0 + timedelta(hours=2)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    worker = ScriptedWorker({})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )

    # Pass raw dicts in capacities list: one valid with reset_at string, one invalid (bad fields)
    event_source.emit(
        Event(
            event_type=EventType.CAPACITY_PROBE,
            timestamp=t0,
            payload={
                "capacities": [
                    {
                        "agent": "claude",
                        "state": "COOLDOWN_SESSION",
                        "observed_at": t0.isoformat(),
                        "source": "provider",
                        "confidence": "high",
                        "reset_at": reset_at.isoformat(),
                    },
                    {"bad": "dictionary without required capacity fields"},
                ]
            },
        )
    )

    assert scheduler.tick() is False
    assert "claude" in scheduler._cooldowns
    assert scheduler._cooldowns["claude"].reset_at == reset_at


def test_scheduler_event_source_task_completed_by_task_id_and_unknown_task(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    event_source = FakeEventSource()

    worker = ScriptedWorker({})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
        event_sources=(event_source,),
    )
    scheduler.discover((Issue(number=1, title="one"),))

    # Advance task 1 to READY_FOR_REVIEW
    t1 = scheduler.queue.get(1)
    t1 = t1.transition(TaskState.DISPATCHED, current_agent="claude", now=t0)
    t1 = t1.transition(TaskState.IN_PROGRESS, current_agent="claude", now=t0)
    t1 = t1.transition(TaskState.VERIFYING, current_agent="claude", now=t0)
    t1 = t1.transition(TaskState.PR_READY, current_agent="claude", now=t0)
    t1 = t1.transition(TaskState.READY_FOR_REVIEW, current_agent="claude", now=t0)
    scheduler.queue = scheduler.queue.replace(t1)
    scheduler._persist()

    # 1. TASK_COMPLETED with unknown issue number
    event_source.emit(
        Event(
            event_type=EventType.TASK_COMPLETED,
            timestamp=t0,
            payload={"issue_number": 999},
        )
    )
    with caplog.at_level(logging.WARNING):
        scheduler.tick()

    assert any("unknown task" in record.message.lower() for record in caplog.records)
    assert scheduler.queue.get(1).status is TaskState.READY_FOR_REVIEW

    # 2. TASK_COMPLETED by task_id matching task 1
    event_source.emit(
        Event(
            event_type=EventType.TASK_COMPLETED,
            timestamp=t0,
            payload={"task_id": t1.task_id},
        )
    )
    scheduler.tick()
    assert scheduler.queue.get(1).status is TaskState.COMPLETE



