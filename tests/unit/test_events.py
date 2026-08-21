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
