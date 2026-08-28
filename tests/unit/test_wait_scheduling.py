from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from subsched.cli import _run_watch_loop
from subsched.events import FakeClock
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


def test_earliest_reset_calculation_and_wait_duration(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    clock = FakeClock(now)
    store = JsonStateStore(tmp_path / "state.json")
    router = Router([AgentConfig("claude", priority=100), AgentConfig("codex", priority=90)])

    t_claude_reset = now + timedelta(hours=1)
    t_codex_reset = now + timedelta(hours=3)

    worker = ScriptedWorker(
        {
            (101, "claude"): (
                AgentResult(AgentResultKind.CAPACITY_SESSION, reset_at=t_claude_reset),
            ),
            (101, "codex"): (
                AgentResult(AgentResultKind.CAPACITY_SESSION, reset_at=t_codex_reset),
            ),
        }
    )

    scheduler = Scheduler(
        store=store,
        router=router,
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
    )
    # When no tasks and no cooldowns, next_reset_at is None
    assert scheduler.next_reset_at() is None
    assert scheduler.wait_duration() == timedelta(0)

    scheduler.discover([Issue(number=101, title="Task 101")])

    cap_claude = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        observed_at=now,
        source="provider",
        confidence="high",
    )
    cap_codex = Capacity(
        agent="codex",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        observed_at=now,
        source="provider",
        confidence="high",
    )

    capacities = [cap_claude, cap_codex]
    scheduler.run_until_waiting(capacities, now=now)

    # Now both agents are in cooldown and scheduler is waiting
    assert scheduler.is_waiting_for_capacity
    assert scheduler.next_reset_at() == t_claude_reset
    wait_dur = scheduler.wait_duration()
    assert abs(wait_dur.total_seconds() - 3600) < 2


def test_watch_loop_waits_until_reset_before_refresh_and_policy_cannot_release(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    clock = FakeClock(now)
    elapsed = 0.0
    worker = ScriptedWorker(
        {
            (101, "claude"): (
                AgentResult(
                    AgentResultKind.CAPACITY_SESSION,
                    reset_at=now + timedelta(seconds=60),
                ),
            )
        }
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router([AgentConfig("claude", priority=100)]),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])
    observations = [
        Capacity(
            agent="claude",
            state=CapacityState.AVAILABLE,
            observed_at=now,
            source="provider",
            confidence="high",
        ),
        Capacity(
            agent="claude",
            state=CapacityState.AVAILABLE,
            observed_at=now + timedelta(seconds=60),
            source="policy",
            confidence="low",
        ),
    ]
    probe_calls = 0

    def capacity_supplier() -> tuple[Capacity, ...]:
        nonlocal probe_calls
        value = observations[min(probe_calls, len(observations) - 1)]
        probe_calls += 1
        return (value,)

    def fake_sleep(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds
        clock.advance(timedelta(seconds=seconds))

    timed_out = _run_watch_loop(
        scheduler,
        capacity_supplier=capacity_supplier,
        watch=True,
        ci_monitoring=False,
        poll_seconds=10,
        timeout_seconds=70,
        sleep=fake_sleep,
        monotonic=lambda: elapsed,
    )

    assert timed_out is True
    assert probe_calls == 2
    assert worker.dispatches == [(101, "claude")]
    assert scheduler.is_waiting_for_capacity is True


def test_watch_loop_interrupt_preserves_waiting_state(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    clock = FakeClock(now)
    store = JsonStateStore(tmp_path / "state.json")
    scheduler = Scheduler(
        store=store,
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker(
            {
                (101, "claude"): (
                    AgentResult(
                        AgentResultKind.CAPACITY_SESSION,
                        reset_at=now + timedelta(minutes=5),
                    ),
                )
            }
        ),
        worktree_root=tmp_path / "worktrees",
        clock=clock,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])
    available = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=now,
        source="provider",
        confidence="high",
    )

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _run_watch_loop(
            scheduler,
            capacity_supplier=lambda: (available,),
            watch=True,
            ci_monitoring=False,
            poll_seconds=10,
            timeout_seconds=60,
            sleep=interrupt,
            monotonic=lambda: 0.0,
        )

    assert scheduler.is_waiting_for_capacity is True
    assert store.load_tasks()[0].status is TaskState.WAITING_CAPACITY


def test_bounded_backoff_when_reset_is_unknown(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    clock = FakeClock(now)
    store = JsonStateStore(tmp_path / "state.json")
    router = Router([AgentConfig("claude", priority=100)])

    # Capacity event with temporary rate limit and unknown reset_at
    c_unknown = Capacity(
        agent="claude",
        state=CapacityState.RATE_LIMITED_TEMPORARY,
        scope="temporary",
        reset_at=None,
        observed_at=now,
        source="output_classification",
        confidence="medium",
    )

    scheduler = Scheduler(
        store=store,
        router=router,
        worker=ScriptedWorker({}),
        worktree_root=tmp_path / "worktrees",
        clock=clock,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([c_unknown])

    assert scheduler.is_waiting_for_capacity
    # Should produce bounded backoff (e.g. between 30s and 900s)
    next_reset = scheduler.next_reset_at()
    assert next_reset is not None
    assert 30 <= (next_reset - now).total_seconds() <= 900


def test_backoff_step_increments_on_repeated_unavailable_ticks(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    clock = FakeClock(now)
    store = JsonStateStore(tmp_path / "state.json")
    router = Router([AgentConfig("claude", priority=100)])

    scheduler = Scheduler(
        store=store,
        router=router,
        worker=ScriptedWorker({}),
        worktree_root=tmp_path / "worktrees",
        clock=clock,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])

    # No capacities available
    assert scheduler.tick([]) is False
    assert scheduler._backoff_step == 1
    dur1 = scheduler.wait_duration()

    assert scheduler.tick([]) is False
    assert scheduler._backoff_step == 2
    dur2 = scheduler.wait_duration()
    assert dur2 > dur1


def test_manual_wake_and_capacity_refresh(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    clock = FakeClock(now)
    store = JsonStateStore(tmp_path / "state.json")
    router = Router([AgentConfig("claude", priority=100)])

    worker = ScriptedWorker(
        {
            (101, "claude"): (
                AgentResult(AgentResultKind.CAPACITY_SESSION, reset_at=now + timedelta(hours=2)),
                AgentResult(AgentResultKind.PASS),
            ),
        }
    )

    cap_avail = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        observed_at=now,
        source="provider",
        confidence="high",
    )

    scheduler = Scheduler(
        store=store,
        router=router,
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        clock=clock,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.run_until_waiting([cap_avail], now=now)
    assert scheduler.is_waiting_for_capacity

    # Supply fresh probe via manual capacity refresh
    clock.advance(timedelta(minutes=5))
    fresh_probe = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        observed_at=clock.now(),
        source="provider",
        confidence="high",
    )
    scheduler.refresh_capacities([fresh_probe])
    scheduler.manual_wake()

    # Next tick runs with the fresh capacity
    ran = scheduler.tick([fresh_probe])
    assert ran is True
    assert scheduler.tasks[0].status == TaskState.COMPLETE


def test_timezone_aware_and_dst_compatibility(tmp_path: Path) -> None:
    # JST timezone observation
    jst = ZoneInfo("Asia/Tokyo")
    jst_time = datetime(2026, 8, 12, 19, 0, tzinfo=jst)
    utc_time = jst_time.astimezone(UTC)
    clock = FakeClock(utc_time)

    store = JsonStateStore(tmp_path / "state.json")
    router = Router([AgentConfig("claude", priority=100)])

    scheduler = Scheduler(
        store=store,
        router=router,
        worker=ScriptedWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)}),
        worktree_root=tmp_path / "worktrees",
        clock=clock,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])

    cap_jst = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        observed_at=jst_time,
        source="provider",
        confidence="high",
    )
    assert scheduler.tick([cap_jst]) is True


def test_clock_jump_and_stale_observations(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    clock = FakeClock(now)
    store = JsonStateStore(tmp_path / "state.json")
    router = Router([AgentConfig("claude", priority=100)])

    scheduler = Scheduler(
        store=store,
        router=router,
        worker=ScriptedWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)}),
        worktree_root=tmp_path / "worktrees",
        clock=clock,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])

    # Backward clock jump
    clock.set_time(now - timedelta(hours=1))
    cap_jump = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        observed_at=clock.now(),
        source="provider",
        confidence="high",
    )
    assert scheduler.tick([cap_jump]) is True
