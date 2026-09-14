from __future__ import annotations

import io
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from subsched.scheduler import Scheduler
from subsched.storage import JsonStateStore
from subsched.structured_logger import StructuredLogger


def _write_handoff(
    worktree: Path,
    issue_number: int,
    *,
    title: str = "Test Task",
    next_action: str = "Verify",
    timestamp: str,
) -> None:
    handoffs_dir = worktree / ".ai" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    (handoffs_dir / f"{issue_number}.md").write_text(
        f"""# Issue

#{issue_number} {title}

## Goal

- Implement {title}

## Current Plan

- Initial approach

## Completed

- Setup

## Current Work

- In progress

## Decisions

- None

## Known Broken State

- None

## Next Action

- {next_action}

## Timestamp

{timestamp}
""",
        encoding="utf-8",
    )


class _ScriptedWorker:
    def __init__(
        self,
        scripts: dict[tuple[int, str], tuple[AgentResult, ...]],
        *,
        update_handoff_at: datetime | None = None,
        handoff_next_action: str | None = None,
    ) -> None:
        self._scripts = {key: deque(results) for key, results in scripts.items()}
        self.update_handoff_at = update_handoff_at
        self.handoff_next_action = handoff_next_action
        self.dispatches: list[tuple[int, str, int]] = []

    def run(self, task: Task, agent: str) -> AgentResult:
        self.dispatches.append((task.issue_number, agent, task.attempt))
        assert task.worktree is not None
        worktree_dir = Path(task.worktree)
        if self.update_handoff_at is not None:
            _write_handoff(
                worktree_dir,
                task.issue_number,
                title=task.title,
                next_action=self.handoff_next_action or "Verify",
                timestamp=self.update_handoff_at.isoformat(),
            )
        return self._scripts[(task.issue_number, agent)].popleft()


def _available(agent: str, *, observed_at: datetime) -> Capacity:
    return Capacity(
        agent=agent,
        state=CapacityState.AVAILABLE,
        reset_at=None,
        observed_at=observed_at,
        source="provider",
        confidence="high",
    )


def _make_scheduler(
    tmp_path: Path,
    worker: _ScriptedWorker,
    *,
    handoff_continuous: bool = True,
    logger: StructuredLogger | None = None,
) -> Scheduler:
    return Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router([AgentConfig("codex", priority=100), AgentConfig("claude", priority=90)]),
        worker=worker,  # type: ignore[arg-type]
        worktree_root=tmp_path / "worktrees",
        handoff_continuous=handoff_continuous,
        max_agent_failures=3,
        structured_logger=logger,
    )


def test_issue_293_reproduction_worker_needs_human_stops_in_one_dispatch(
    tmp_path: Path,
) -> None:
    """#297 (Issue #293 reproduction): Worker reporting NEEDS_HUMAN with fresh handoff
    halts in exactly 1 dispatch without retrying or burning attempts/failure counts."""
    start_time = datetime(2026, 9, 13, 11, 44, tzinfo=UTC)
    fresh_time = start_time + timedelta(minutes=2)

    result = AgentResult(
        AgentResultKind.NEEDS_HUMAN,
        reason_code="operator_decision_required",
        output="container isolation design approval required",
    )
    worker = _ScriptedWorker(
        {(293, "codex"): (result,)},
        update_handoff_at=fresh_time,
    )
    scheduler = _make_scheduler(tmp_path, worker, handoff_continuous=True)
    scheduler.discover([Issue(number=293, title="Container isolation design")])

    # First tick: dispatches attempt 0 to codex
    ticked = scheduler.tick([_available("codex", observed_at=start_time)], now=start_time)
    assert ticked is True
    assert len(worker.dispatches) == 1
    assert worker.dispatches[0] == (293, "codex", 0)

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.attempt == 0
    assert task.per_agent_failures == ()
    assert task.needs_human_reason is not None
    assert "operator_decision_required" in task.needs_human_reason
    assert "container isolation design approval required" in task.needs_human_reason

    # Second tick: no more runnable tasks, no retry occurs!
    ticked_again = scheduler.tick([_available("codex", observed_at=start_time)], now=start_time)
    assert ticked_again is False
    assert len(worker.dispatches) == 1


def test_worker_needs_human_with_stale_handoff_escalates_and_logs_reason_code(
    tmp_path: Path,
) -> None:
    """#297: Worker reporting NEEDS_HUMAN with stale handoff fails closed to NEEDS_HUMAN,
    logging both the handoff readback error and the original reason_code, without retry."""
    start_time = datetime(2026, 9, 13, 11, 44, tzinfo=UTC)

    result = AgentResult(
        AgentResultKind.NEEDS_HUMAN,
        reason_code="external_prerequisite",
        output="need external service provisioned",
    )
    # update_handoff_at is None -> handoff remains at dispatch timestamp (stale)
    worker = _ScriptedWorker({(294, "claude"): (result,)})

    log_buffer = io.StringIO()
    logger = StructuredLogger(log_buffer)
    scheduler = _make_scheduler(tmp_path, worker, handoff_continuous=True, logger=logger)
    scheduler.discover([Issue(number=294, title="External dep task")])

    ticked = scheduler.tick([_available("claude", observed_at=start_time)], now=start_time)
    assert ticked is True
    assert len(worker.dispatches) == 1

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.attempt == 0
    assert task.per_agent_failures == ()
    assert task.needs_human_reason is not None
    assert "did not advance" in task.needs_human_reason

    # Check structured log
    log_content = log_buffer.getvalue()
    assert "handoff_readback" in log_content
    assert "external_prerequisite" in log_content
    assert "NEEDS_HUMAN" in log_content

    # Confirm no retry
    assert scheduler.tick([_available("claude", observed_at=start_time)], now=start_time) is False
    assert len(worker.dispatches) == 1


def test_generic_failure_with_fresh_handoff_retries_normally(
    tmp_path: Path,
) -> None:
    """#297: Unlike NEEDS_HUMAN, generic FAILURE with fresh handoff consumes retry budget."""
    start_time = datetime(2026, 9, 13, 11, 44, tzinfo=UTC)
    fresh_time = start_time + timedelta(minutes=2)

    failure_result = AgentResult(AgentResultKind.FAILURE, output="build error")
    worker = _ScriptedWorker(
        {(295, "codex"): (failure_result, failure_result)},
        update_handoff_at=fresh_time,
    )
    scheduler = _make_scheduler(tmp_path, worker, handoff_continuous=True)
    scheduler.discover([Issue(number=295, title="Transient failure")])

    # First tick: attempt 0 fails
    ticked = scheduler.tick([_available("codex", observed_at=start_time)], now=start_time)
    assert ticked is True
    task = scheduler.tasks[0]
    # Retries to READY
    assert task.status is TaskState.READY
    assert task.attempt == 1
    assert dict(task.per_agent_failures) == {"codex": 1}


def test_generic_failure_with_stale_handoff_escalates_immediately(
    tmp_path: Path,
) -> None:
    """#297 / #145: Generic FAILURE with stale handoff escalates immediately to NEEDS_HUMAN."""
    start_time = datetime(2026, 9, 13, 11, 44, tzinfo=UTC)

    failure_result = AgentResult(AgentResultKind.FAILURE, output="build error")
    worker = _ScriptedWorker({(296, "codex"): (failure_result,)})
    scheduler = _make_scheduler(tmp_path, worker, handoff_continuous=True)
    scheduler.discover([Issue(number=296, title="Stale failure")])

    ticked = scheduler.tick([_available("codex", observed_at=start_time)], now=start_time)
    assert ticked is True
    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert "did not advance" in (task.needs_human_reason or "")


def test_worker_needs_human_falls_back_to_handoff_next_action_when_summary_empty(
    tmp_path: Path,
) -> None:
    """#297: When worker summary is empty, Scheduler uses handoff Next Action."""
    start_time = datetime(2026, 9, 13, 11, 44, tzinfo=UTC)
    fresh_time = start_time + timedelta(minutes=2)

    result = AgentResult(
        AgentResultKind.NEEDS_HUMAN,
        reason_code="instruction_conflict",
        output="",
    )
    worker = _ScriptedWorker(
        {(297, "codex"): (result,)},
        update_handoff_at=fresh_time,
        handoff_next_action="Operator clarify conflicting timeout specs",
    )
    scheduler = _make_scheduler(tmp_path, worker, handoff_continuous=True)
    scheduler.discover([Issue(number=297, title="Conflict task")])

    ticked = scheduler.tick([_available("codex", observed_at=start_time)], now=start_time)
    assert ticked is True

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.needs_human_reason is not None
    assert "instruction_conflict" in task.needs_human_reason
    assert "Operator clarify conflicting timeout specs" in task.needs_human_reason


def test_worker_needs_human_persists_and_restores_across_restart(
    tmp_path: Path,
) -> None:
    """#297: NEEDS_HUMAN durable reason survives scheduler restart."""
    start_time = datetime(2026, 9, 13, 11, 44, tzinfo=UTC)
    fresh_time = start_time + timedelta(minutes=2)

    result = AgentResult(
        AgentResultKind.NEEDS_HUMAN,
        reason_code="operator_decision_required",
        output="approve container isolation architecture",
    )
    worker = _ScriptedWorker(
        {(298, "codex"): (result,)},
        update_handoff_at=fresh_time,
    )
    scheduler = _make_scheduler(tmp_path, worker, handoff_continuous=True)
    scheduler.discover([Issue(number=298, title="Architecture review")])
    scheduler.tick([_available("codex", observed_at=start_time)], now=start_time)

    # Simulate restart by loading new Scheduler from same store
    restarted = _make_scheduler(tmp_path, worker, handoff_continuous=True)
    assert len(restarted.tasks) == 1
    recovered_task = restarted.tasks[0]
    assert recovered_task.status is TaskState.NEEDS_HUMAN
    assert recovered_task.attempt == 0
    assert recovered_task.needs_human_reason is not None
    assert "operator_decision_required" in recovered_task.needs_human_reason
    assert "approve container isolation architecture" in recovered_task.needs_human_reason
