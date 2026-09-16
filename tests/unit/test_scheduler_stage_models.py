from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.config import AgentEffortPolicy, AgentModelPolicy, AgentSettings, WorkflowConfig
from subsched.models import (
    AgentResult,
    AgentResultKind,
    Capacity,
    CapacityState,
    Issue,
    Task,
    TaskState,
    resolve_stage,
)
from subsched.plan_review import plan_path
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore
from subsched.structured_logger import StructuredLogger


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


class RecordingPlanWritingWorker:
    def __init__(self, scripts: dict[tuple[int, str], tuple[AgentResult, ...]]) -> None:
        self._inner = ScriptedWorker(scripts)
        self.recorded_tasks: list[Task] = []

    @property
    def dispatches(self) -> list[tuple[int, str]]:
        return self._inner.dispatches

    def run(self, task: Task, agent: str) -> AgentResult:
        self.recorded_tasks.append(task)
        result = self._inner.run(task, agent)
        if task.status is TaskState.PLANNING and result.kind is AgentResultKind.PASS:
            assert task.worktree is not None
            plan_file = Path(task.worktree) / plan_path(task.issue_number)
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text("# Plan\n", encoding="utf-8")
        return result


def test_task_stage_resolution_and_effective_model() -> None:
    task = Task.from_issue(Issue(number=1, title="test"))
    assert resolve_stage(task) == "implementation"
    assert task.effective_model == "provider-default"
    assert task.dispatch_model is None
    assert task.dispatch_stage is None

    planning = task.transition(TaskState.DISPATCHED).transition(TaskState.PLANNING)
    assert resolve_stage(planning) == "planning"

    review = planning.transition(TaskState.PLAN_REVIEW)
    assert resolve_stage(review) == "plan_review"

    pr_review = Task(
        task_id="github-1",
        issue_number=1,
        title="test",
        labels=(),
        status=TaskState.IN_PROGRESS,
        dispatch_status=TaskState.PR_REVIEW,
    )
    assert resolve_stage(pr_review) == "pr_review"

    revision = Task(
        task_id="github-1",
        issue_number=1,
        title="test",
        labels=(),
        status=TaskState.IN_PROGRESS,
        dispatch_status=TaskState.REVISING,
    )
    assert resolve_stage(revision) == "revision"


def test_task_model_serialization_backward_compatibility() -> None:
    task = Task.from_issue(Issue(number=1, title="test"))
    serialized = task.to_dict()
    assert "dispatch_stage" in serialized
    assert "dispatch_model" in serialized
    assert serialized["dispatch_stage"] is None
    assert serialized["dispatch_model"] is None

    # Backward compatibility: legacy payload without dispatch_stage / dispatch_model
    del serialized["dispatch_stage"]
    del serialized["dispatch_model"]
    restored = Task.from_dict(serialized)
    assert restored.dispatch_stage is None
    assert restored.dispatch_model is None
    assert restored.effective_model == "provider-default"

    # Explicit stage and model
    explicit = Task(
        task_id="github-1",
        issue_number=1,
        title="test",
        labels=(),
        status=TaskState.IN_PROGRESS,
        dispatch_stage="planning",
        dispatch_model="claude-opus-4",
    )
    assert explicit.effective_model == "claude-opus-4"
    restored_explicit = Task.from_dict(explicit.to_dict())
    assert restored_explicit.dispatch_stage == "planning"
    assert restored_explicit.dispatch_model == "claude-opus-4"
    assert restored_explicit.effective_model == "claude-opus-4"


def test_scheduler_stage_model_dispatch_and_logging(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    log_file = tmp_path / "events.jsonl"
    logger = StructuredLogger(log_file)

    models = AgentModelPolicy(
        default="sonnet",
        planning="opus",
        plan_review="opus",
        implementation="sonnet",
    )
    agents = {"claude": AgentSettings(enabled=True, priority=100, models=models)}

    worker = RecordingPlanWritingWorker(
        {
            (1, "claude"): (
                AgentResult(AgentResultKind.PASS),  # PLANNING
                AgentResult(  # PLAN_REVIEW: approve
                    AgentResultKind.PASS, output='{"verdict": "APPROVE", "summary": "ok"}'
                ),
                AgentResult(AgentResultKind.PASS),  # IN_PROGRESS
            )
        }
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        workflow=WorkflowConfig(mode="multi-stage"),
        agents=agents,
        structured_logger=logger,
    )
    scheduler.discover((Issue(number=1, title="one"),))
    scheduler.run_until_waiting((available("claude", now),), now=now)

    # Inspect recorded tasks received by worker
    assert len(worker.recorded_tasks) == 3
    assert worker.recorded_tasks[0].dispatch_stage == "planning"
    assert worker.recorded_tasks[0].dispatch_model == "opus"
    assert worker.recorded_tasks[1].dispatch_stage == "plan_review"
    assert worker.recorded_tasks[1].dispatch_model == "opus"
    assert worker.recorded_tasks[2].dispatch_stage == "implementation"
    assert worker.recorded_tasks[2].dispatch_model == "sonnet"

    # Verify structured dispatch events
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    dispatches = [e for e in events if e["event"] == "dispatch"]
    assert len(dispatches) == 3

    assert dispatches[0]["data"]["stage"] == "planning"
    assert dispatches[0]["data"]["model"] == "opus"
    assert dispatches[0]["data"]["agent"] == "claude"

    assert dispatches[1]["data"]["stage"] == "plan_review"
    assert dispatches[1]["data"]["model"] == "opus"
    assert dispatches[1]["data"]["agent"] == "claude"

    assert dispatches[2]["data"]["stage"] == "implementation"
    assert dispatches[2]["data"]["model"] == "sonnet"
    assert dispatches[2]["data"]["agent"] == "claude"


def test_scheduler_unconfigured_model_logs_provider_default(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    log_file = tmp_path / "events.jsonl"
    logger = StructuredLogger(log_file)

    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        structured_logger=logger,
    )
    scheduler.discover((Issue(number=1, title="one"),))
    scheduler.run_until_waiting((available("claude", now),), now=now)

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    dispatches = [e for e in events if e["event"] == "dispatch"]
    assert len(dispatches) == 1
    assert dispatches[0]["data"]["stage"] == "implementation"
    assert dispatches[0]["data"]["model"] == "provider-default"


def test_scheduler_stage_effort_dispatch_and_logging(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    log_file = tmp_path / "events.jsonl"
    logger = StructuredLogger(log_file)

    effort = AgentEffortPolicy(
        default="medium",
        planning="high",
        plan_review="high",
        implementation="medium",
    )
    agents = {"claude": AgentSettings(enabled=True, priority=100, effort=effort)}

    worker = RecordingPlanWritingWorker(
        {
            (1, "claude"): (
                AgentResult(AgentResultKind.PASS),  # PLANNING
                AgentResult(  # PLAN_REVIEW: approve
                    AgentResultKind.PASS, output='{"verdict": "APPROVE", "summary": "ok"}'
                ),
                AgentResult(AgentResultKind.PASS),  # IN_PROGRESS
            )
        }
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        workflow=WorkflowConfig(mode="multi-stage"),
        agents=agents,
        structured_logger=logger,
    )
    scheduler.discover((Issue(number=1, title="one"),))
    scheduler.run_until_waiting((available("claude", now),), now=now)

    # Inspect recorded tasks received by worker
    assert len(worker.recorded_tasks) == 3
    assert worker.recorded_tasks[0].dispatch_stage == "planning"
    assert worker.recorded_tasks[0].dispatch_effort == "high"
    assert worker.recorded_tasks[1].dispatch_stage == "plan_review"
    assert worker.recorded_tasks[1].dispatch_effort == "high"
    assert worker.recorded_tasks[2].dispatch_stage == "implementation"
    assert worker.recorded_tasks[2].dispatch_effort == "medium"

    # Verify structured dispatch events
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    dispatches = [e for e in events if e["event"] == "dispatch"]
    assert len(dispatches) == 3

    assert dispatches[0]["data"]["stage"] == "planning"
    assert dispatches[0]["data"]["effort"] == "high"
    assert dispatches[0]["data"]["agent"] == "claude"

    assert dispatches[1]["data"]["stage"] == "plan_review"
    assert dispatches[1]["data"]["effort"] == "high"
    assert dispatches[1]["data"]["agent"] == "claude"

    assert dispatches[2]["data"]["stage"] == "implementation"
    assert dispatches[2]["data"]["effort"] == "medium"
    assert dispatches[2]["data"]["agent"] == "claude"


def test_scheduler_unconfigured_effort_logs_provider_default(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    log_file = tmp_path / "events.jsonl"
    logger = StructuredLogger(log_file)

    worker = ScriptedWorker({(1, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        structured_logger=logger,
    )
    scheduler.discover((Issue(number=1, title="one"),))
    scheduler.run_until_waiting((available("claude", now),), now=now)

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    dispatches = [e for e in events if e["event"] == "dispatch"]
    assert len(dispatches) == 1
    assert dispatches[0]["data"]["stage"] == "implementation"
    assert dispatches[0]["data"]["effort"] == "provider-default"
