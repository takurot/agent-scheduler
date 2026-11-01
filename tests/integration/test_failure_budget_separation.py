from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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


def test_verification_failure_stage_reaches_retry_checkpoint_and_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verification
    from subsched.agents.base import ProcessExecutionResult
    from subsched.checkpoint import load_checkpoint
    from subsched.contract import build_worker_prompt

    canary = "PRIVATE_UNFORMATTED_SECRET_CANARY"
    monkeypatch.setattr(
        verification, "run_process_group",
        lambda request: ProcessExecutionResult(
            exit_code=1,
            stdout="=== Running Ruff linter ===\n" + canary,
            stderr=canary,
        ),
    )
    now = datetime(2026, 8, 28, tzinfo=UTC)
    seen: list[str] = []

    class Worker:
        def run(self, task, agent):  # type: ignore[no-untyped-def]
            assert task.worktree is not None
            checkpoint = load_checkpoint(Path(task.worktree), task.issue_number)
            if checkpoint is not None:
                seen.append(checkpoint.test_results)
                assert f".ai/checkpoints/{task.issue_number}.json" in build_worker_prompt(task)
                return AgentResult(AgentResultKind.FAILURE)
            return AgentResult(AgentResultKind.PASS)

    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router((AgentConfig("claude", 100),)), worker=Worker(),
        worktree_root=tmp_path / "worktrees", worktree_adapter=None,
        verification_commands=("bash scripts/quality_gate.sh",), max_agent_failures=2,
    )
    scheduler.discover((Issue(number=1, title="one"),))
    assert scheduler.tick((_available("claude", now),), now=now)
    assert scheduler.tasks[0].status is TaskState.READY
    assert scheduler.tick((_available("claude", now),), now=now)
    assert len(seen) == 1
    assert "reported stage: ruff" in seen[0]
    assert canary not in seen[0]


def test_terminal_verification_failure_exposes_safe_stage_to_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verification
    from subsched.agents.base import ProcessExecutionResult

    canary = "PRIVATE_TERMINAL_CANARY"
    monkeypatch.setattr(
        verification, "run_process_group",
        lambda request: ProcessExecutionResult(
            exit_code=1, stdout="=== Running Mypy type checker ===\n" + canary, stderr=canary
        ),
    )
    now = datetime(2026, 8, 28, tzinfo=UTC)
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router((AgentConfig("claude", 100),)),
        worker=ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)}),
        worktree_root=tmp_path / "worktrees", worktree_adapter=None,
        verification_commands=("bash scripts/quality_gate.sh",), max_verification_failures=1,
    )
    scheduler.discover((Issue(number=1, title="one"),))
    assert scheduler.tick((_available("claude", now),), now=now)
    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert "reported stage: mypy" in (task.needs_human_reason or "")
    assert canary not in (task.needs_human_reason or "")
    assert canary not in scheduler.store.path.read_text(encoding="utf-8")
    assert task.worktree is not None
    checkpoint = Path(task.worktree) / ".ai/checkpoints/1.json"
    assert "reported stage: mypy" in checkpoint.read_text(encoding="utf-8")
    assert canary not in checkpoint.read_text(encoding="utf-8")
