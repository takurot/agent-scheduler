from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.models import AgentResult, AgentResultKind, Capacity, CapacityState, Issue, TaskState
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore

# Regression coverage for #175: `_handle_result()` escalated to NEEDS_HUMAN based on
# `task.attempt` (a task-wide retry counter shared by verification failures and Agent
# failures) instead of the per-agent failure count that `max_agent_failures` is
# documented (docs/SPEC.md #50) and named to gate. A verification failure that
# incremented `task.attempt` could push a task to NEEDS_HUMAN after a single, genuine
# first-time Agent failure.


def _available(agent: str, now: datetime) -> Capacity:
    return Capacity(
        agent=agent,
        state=CapacityState.AVAILABLE,
        reset_at=now + timedelta(hours=5),
        observed_at=now,
        source="provider",
        confidence="high",
    )


def test_verification_failure_then_first_agent_failure_does_not_escalate(
    tmp_path: Path,
) -> None:
    """max_agent_failures=2. One verification failure (task.attempt -> 1) followed by
    one genuine Agent failure (per_agent_failures['claude'] -> 1) must stay READY: the
    Agent has only failed once, even though task.attempt has advanced twice."""
    now = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    worker = ScriptedWorker(
        {
            (1, "claude"): (
                AgentResult(AgentResultKind.PASS),
                AgentResult(AgentResultKind.FAILURE, output="boom"),
            ),
        }
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        worktree_adapter=None,
        verification_commands=("false",),
        max_agent_failures=2,
    )
    scheduler.discover((Issue(number=1, title="one"),))

    # Tick 1: Agent PASSes but verification fails -> RETRY -> READY, task.attempt == 1.
    assert scheduler.tick((_available("claude", now),), now=now) is True
    task = scheduler.tasks[0]
    assert task.status is TaskState.READY
    assert task.attempt == 1
    assert task.verification_failures == 1
    assert dict(task.per_agent_failures) == {}

    # Tick 2: Agent itself fails for the first time. Must NOT escalate: per-agent
    # failure count for claude is only 1, below max_agent_failures=2.
    assert scheduler.tick((_available("claude", now),), now=now) is True
    task = scheduler.tasks[0]
    assert task.status is TaskState.READY
    assert dict(task.per_agent_failures) == {"claude": 1}


def test_verification_failures_escalate_independently_of_agent_failures(
    tmp_path: Path,
) -> None:
    """Repeated verification failures (Agent PASSes every time) must still escalate to
    NEEDS_HUMAN via their own budget, without ever touching per_agent_failures."""
    now = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    worker = ScriptedWorker(
        {
            (1, "claude"): (
                AgentResult(AgentResultKind.PASS),
                AgentResult(AgentResultKind.PASS),
            ),
        }
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        worktree_adapter=None,
        verification_commands=("false",),
        max_verification_failures=2,
    )
    scheduler.discover((Issue(number=1, title="one"),))

    assert scheduler.tick((_available("claude", now),), now=now) is True
    task = scheduler.tasks[0]
    assert task.status is TaskState.READY
    assert task.verification_failures == 1

    assert scheduler.tick((_available("claude", now),), now=now) is True
    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.verification_failures == 2
    assert dict(task.per_agent_failures) == {}
