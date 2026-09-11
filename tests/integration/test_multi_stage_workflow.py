"""Integration tests for the multi-stage PLANNING -> PLAN_REVIEW gate (issue #280).

Uses ScriptedWorker (wrapped to also write the plan file a real PLANNING-stage agent
would produce) to simulate plan approval and rejection cycles end-to-end through the
Scheduler's dispatch loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.config import WorkflowConfig, WorkflowLimitsConfig
from subsched.models import (
    AgentResult,
    AgentResultKind,
    Capacity,
    CapacityState,
    Issue,
    Task,
    TaskState,
)
from subsched.plan_review import plan_path
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


class PlanWritingWorker:
    """Wraps ScriptedWorker, additionally writing the plan file a real PLANNING-stage
    agent would produce whenever a scripted PLANNING result is a PASS."""

    def __init__(self, scripts: dict[tuple[int, str], tuple[AgentResult, ...]]) -> None:
        self._inner = ScriptedWorker(scripts)

    @property
    def dispatches(self) -> list[tuple[int, str]]:
        return self._inner.dispatches

    def run(self, task: Task, agent: str) -> AgentResult:
        result = self._inner.run(task, agent)
        if task.status is TaskState.PLANNING and result.kind is AgentResultKind.PASS:
            assert task.worktree is not None
            plan_file = Path(task.worktree) / plan_path(task.issue_number)
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text("# Plan\n", encoding="utf-8")
        return result


def _scheduler(tmp_path: Path, worker: PlanWritingWorker, **kwargs: object) -> Scheduler:
    return Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        workflow=WorkflowConfig(mode="multi-stage"),
        **kwargs,  # type: ignore[arg-type]
    )


def test_plan_approved_on_first_review_proceeds_to_completion(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    worker = PlanWritingWorker(
        {
            (1, "claude"): (
                AgentResult(AgentResultKind.PASS),  # PLANNING
                AgentResult(  # PLAN_REVIEW
                    AgentResultKind.PASS, output='{"verdict": "APPROVE", "summary": "ok"}'
                ),
                AgentResult(AgentResultKind.PASS),  # IN_PROGRESS
            )
        }
    )
    scheduler = _scheduler(tmp_path, worker)
    scheduler.discover((Issue(number=1, title="one"),))

    scheduler.run_until_waiting((available("claude", now),), now=now)

    task = scheduler.tasks[0]
    assert task.status is TaskState.COMPLETE
    assert task.plan_approved is True
    assert task.plan_revisions == 0
    assert worker.dispatches == [(1, "claude"), (1, "claude"), (1, "claude")]


def test_plan_review_request_changes_then_approve(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    worker = PlanWritingWorker(
        {
            (1, "claude"): (
                AgentResult(AgentResultKind.PASS),  # PLANNING (attempt 1)
                AgentResult(  # PLAN_REVIEW: request changes
                    AgentResultKind.PASS,
                    output='{"verdict": "REQUEST_CHANGES", "summary": "needs tests"}',
                ),
                AgentResult(AgentResultKind.PASS),  # PLANNING (attempt 2)
                AgentResult(  # PLAN_REVIEW: approve
                    AgentResultKind.PASS, output='{"verdict": "APPROVE", "summary": "ok"}'
                ),
                AgentResult(AgentResultKind.PASS),  # IN_PROGRESS
            )
        }
    )
    scheduler = _scheduler(tmp_path, worker)
    scheduler.discover((Issue(number=1, title="one"),))

    scheduler.run_until_waiting((available("claude", now),), now=now)

    task = scheduler.tasks[0]
    assert task.status is TaskState.COMPLETE
    assert task.plan_approved is True
    assert task.plan_revisions == 1


def test_plan_review_exceeding_max_revisions_escalates_to_needs_human(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    reject = AgentResult(
        AgentResultKind.PASS,
        output='{"verdict": "REQUEST_CHANGES", "summary": "still not right"}',
    )
    worker = PlanWritingWorker(
        {
            (1, "claude"): (
                AgentResult(AgentResultKind.PASS),  # PLANNING (attempt 1)
                reject,  # PLAN_REVIEW: reject -> plan_revisions=1
                AgentResult(AgentResultKind.PASS),  # PLANNING (attempt 2)
                reject,  # PLAN_REVIEW: reject -> plan_revisions=2, >= limit
            )
        }
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        workflow=WorkflowConfig(
            mode="multi-stage", limits=WorkflowLimitsConfig(max_plan_revisions=2)
        ),
    )
    scheduler.discover((Issue(number=1, title="one"),))

    scheduler.run_until_waiting((available("claude", now),), now=now)

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.plan_revisions == 2
