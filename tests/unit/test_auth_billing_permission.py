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
        reset_at=now + timedelta(hours=5),
        observed_at=now,
        source="provider",
        confidence="high",
    )


def test_permission_denied_escalates_directly_to_needs_human_without_retry(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    worker = ScriptedWorker(
        {
            (1, "claude"): (
                AgentResult(AgentResultKind.PERMISSION_DENIED, output="cannot write branch"),
            )
        }
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        max_agent_failures=3,
    )
    scheduler.discover((Issue(number=1, title="one"),))

    scheduler.tick((_available("claude", now),), now=now)

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert dict(task.per_agent_failures) == {}
    assert task.attempt == 0
    assert "permission denied" in (task.needs_human_reason or "").lower()
    # Claude itself is not disabled for other tasks
    assert "claude" not in scheduler._cooldowns


def test_auth_error_with_alternative_agent_fails_over(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    worker = ScriptedWorker(
        {
            (1, "claude"): (AgentResult(AgentResultKind.AUTH_ERROR, output="token expired"),),
            (1, "codex"): (AgentResult(AgentResultKind.PASS),),
        }
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router((AgentConfig("claude", 100), AgentConfig("codex", 90))),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
    )
    scheduler.discover((Issue(number=1, title="one"),))

    # Tick 1: claude fails with AUTH_ERROR -> claude disabled, task requeued to READY
    assert scheduler.tick(
        (_available("claude", now), _available("codex", now)),
        now=now,
    ) is True

    assert scheduler._cooldowns["claude"].state is CapacityState.AUTH_ERROR
    task = scheduler.tasks[0]
    assert task.status is TaskState.READY
    assert dict(task.per_agent_failures) == {}

    # Tick 2: codex is selected and runs to completion
    assert scheduler.tick(
        (_available("claude", now), _available("codex", now)),
        now=now,
    ) is True

    task = scheduler.tasks[0]
    assert task.status is TaskState.COMPLETE
    assert task.last_dispatched_agent == "codex"
    assert dict(task.per_agent_failures) == {}


def test_auth_error_without_alternative_agent_fails_closed_to_needs_human(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    worker = ScriptedWorker(
        {(1, "claude"): (AgentResult(AgentResultKind.AUTH_ERROR, output="invalid api key"),)}
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
    )
    scheduler.discover((Issue(number=1, title="one"),))

    scheduler.tick((_available("claude", now),), now=now)

    assert scheduler._cooldowns["claude"].state is CapacityState.AUTH_ERROR
    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert dict(task.per_agent_failures) == {}
    assert "no alternative agent" in (task.needs_human_reason or "").lower()


def test_billing_error_and_unknown_billing_disables_agent(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    worker = ScriptedWorker(
        {
            (1, "claude"): (AgentResult(AgentResultKind.BILLING_ERROR, output="payment required"),),
            (1, "codex"): (AgentResult(AgentResultKind.PASS),),
            (2, "codex"): (
                AgentResult(AgentResultKind.UNKNOWN_BILLING, output="cannot determine billing"),
            ),
        }
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router((AgentConfig("claude", 100), AgentConfig("codex", 90))),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
    )
    scheduler.discover((Issue(number=1, title="one"), Issue(number=2, title="two")))

    # Tick 1: Task 1 on claude -> BILLING_ERROR -> disabled_billing, failover to codex
    scheduler.tick((_available("claude", now), _available("codex", now)), now=now)
    assert scheduler._cooldowns["claude"].state is CapacityState.DISABLED_BILLING
    assert scheduler.tasks[0].status is TaskState.READY

    # Tick 2: Task 1 on codex -> PASS -> COMPLETE
    scheduler.tick((_available("claude", now), _available("codex", now)), now=now)
    assert scheduler.tasks[0].status is TaskState.COMPLETE

    # Tick 3: Task 2 on codex -> UNKNOWN_BILLING -> codex disabled.
    # claude is already DISABLED_BILLING, so no alternative agent is available -> NEEDS_HUMAN
    scheduler.tick((_available("claude", now), _available("codex", now)), now=now)
    assert scheduler._cooldowns["codex"].state is CapacityState.DISABLED_BILLING
    task2 = next(t for t in scheduler.tasks if t.issue_number == 2)
    assert task2.status is TaskState.NEEDS_HUMAN
    assert dict(task2.per_agent_failures) == {}


def test_auth_error_and_disabled_billing_survive_restart_and_block_dispatch(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    store = JsonStateStore(tmp_path / "state.json")
    worker = ScriptedWorker(
        {(1, "claude"): (AgentResult(AgentResultKind.AUTH_ERROR, output="unauthorized"),)}
    )
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
    )
    scheduler.discover((Issue(number=1, title="one"),))
    scheduler.tick((_available("claude", now),), now=now)
    assert scheduler._cooldowns["claude"].state is CapacityState.AUTH_ERROR

    # Restart scheduler with a fresh instance loading the same state store
    recovered_scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
    )
    assert recovered_scheduler._cooldowns["claude"].state is CapacityState.AUTH_ERROR

    # Refresh capacities with provider probe claiming AVAILABLE must NOT unblock AUTH_ERROR
    recovered_scheduler.refresh_capacities((_available("claude", now),), now=now)
    assert recovered_scheduler._cooldowns["claude"].state is CapacityState.AUTH_ERROR
