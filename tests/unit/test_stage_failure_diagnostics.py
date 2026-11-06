"""Durable, bounded diagnostics for pre-implementation worker failures (issue #553)."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from subsched.checkpoint import load_checkpoint
from subsched.config import WorkflowConfig
from subsched.models import AgentResult, AgentResultKind, Issue, Task, TaskState
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore
from subsched.structured_logger import StructuredLogger

NOW = datetime(2026, 11, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    "stage", [TaskState.PLANNING, TaskState.PLAN_REVIEW, TaskState.IN_PROGRESS]
)
@pytest.mark.parametrize(
    ("kind", "output", "classification"),
    [
        (AgentResultKind.FAILURE, "synthetic planner diagnostic", "FAILURE"),
        (AgentResultKind.TIMEOUT, "timeout diagnostic", "TIMEOUT"),
        (AgentResultKind.FAILURE, "codex event stream malformed", "malformed_event_stream"),
        (AgentResultKind.UNKNOWN, "unknown diagnostic", "UNKNOWN"),
        (
            AgentResultKind.FAILURE,
            "ghp_SYNTHETICSECRET /home/operator/private " + "界" * 70000,
            "FAILURE",
        ),
    ],
)
def test_failure_diagnostics_survive_restart(
    tmp_path: Path, stage: TaskState, kind: AgentResultKind, output: str, classification: str
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    log = io.StringIO()
    store = JsonStateStore(tmp_path)
    worker = ScriptedWorker({(1, "codex"): (AgentResult(kind, output=output),)})
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("codex", 100),)),
        worker=worker,
        workflow=WorkflowConfig(mode="multi-stage"),
        max_agent_failures=1,
        structured_logger=StructuredLogger(log),
        worktree_root=tmp_path / "worktrees",
    )
    task = Task.from_issue(Issue(number=1, title="diagnostics"), worktree=str(worktree))
    task = task.transition(TaskState.DISPATCHED, current_agent="codex", now=NOW)
    scheduler.queue = scheduler.queue.append([task])
    if stage is TaskState.PLANNING:
        scheduler._run_planning_and_review(task, "codex", NOW, "test-lease")
    else:
        if stage is TaskState.PLAN_REVIEW:
            task = task.transition(TaskState.PLANNING, current_agent="codex", now=NOW)
        task = task.transition(stage, current_agent="codex", now=NOW)
        scheduler.queue = scheduler.queue.replace(task)
        result = scheduler._run_stage_worker(task, "codex", "test-lease")
        scheduler._handle_result(task, "codex", result, NOW)

    restored = Scheduler(
        store=store,
        router=scheduler.router,
        worker=ScriptedWorker({}),
        worktree_root=tmp_path / "worktrees",
    )
    stopped = restored.queue.get(1)
    assert stopped.status is TaskState.NEEDS_HUMAN
    reason = stopped.needs_human_reason or ""
    assert ("implementation" if stage is TaskState.IN_PROGRESS else stage.value.lower()) in reason
    assert "codex" in reason
    assert classification in reason
    assert len(reason.encode()) < 500
    assert stopped.attempt == (0 if stage is TaskState.PLAN_REVIEW else 1)
    assert stopped.per_agent_failures == (() if stage is TaskState.PLAN_REVIEW else (("codex", 1),))
    assert stopped.capacity_events == stopped.agent_switches == 0
    cp = load_checkpoint(worktree, 1)
    assert cp is not None
    assert cp.failure_classification == kind.value
    assert classification in cp.last_agent_output
    assert len(cp.last_agent_output.encode()) < 500
    events = [json.loads(line) for line in log.getvalue().splitlines()]
    finishes = [event for event in events if event["event"] == "agent_finish"]
    assert len(finishes) == 1
    assert finishes[0]["data"]["stage"] == (
        "implementation" if stage is TaskState.IN_PROGRESS else stage.value.lower()
    )
    assert finishes[0]["data"]["failure_classification"] == classification
    assert finishes[0]["data"]["attempt"] == 0
    for persisted in (reason, json.dumps(cp.to_dict()), log.getvalue()):
        assert "SYNTHETICSECRET" not in persisted
        assert "/home/operator/private" not in persisted
        assert "synthetic planner diagnostic" not in persisted


def test_planning_retry_history_is_recorded_before_limit(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    log = io.StringIO()
    worker = ScriptedWorker({(1, "codex"): (AgentResult(AgentResultKind.FAILURE),) * 2})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("codex", 100),)),
        worker=worker,
        workflow=WorkflowConfig(mode="multi-stage"),
        max_agent_failures=2,
        structured_logger=StructuredLogger(log),
        worktree_root=tmp_path / "worktrees",
    )
    task = Task.from_issue(Issue(number=1, title="retry"), worktree=str(worktree))
    scheduler.queue = scheduler.queue.append([task])
    for attempt in range(2):
        task = scheduler.queue.get(1).transition(
            TaskState.DISPATCHED, current_agent="codex", now=NOW
        )
        scheduler.queue = scheduler.queue.replace(task)
        scheduler._run_planning_and_review(task, "codex", NOW, "test-lease")
        assert load_checkpoint(worktree, 1) is not None
        assert scheduler.queue.get(1).status is (
            TaskState.READY if attempt == 0 else TaskState.NEEDS_HUMAN
        )
    finishes = [
        json.loads(line)
        for line in log.getvalue().splitlines()
        if json.loads(line)["event"] == "agent_finish"
    ]
    assert [event["data"]["attempt"] for event in finishes] == [0, 1]


def test_stage_exception_records_safe_failure(tmp_path: Path) -> None:
    class RaisingWorker:
        def run(self, task: Task, agent: str) -> AgentResult:
            raise RuntimeError(
                "synthetic exception detail /home/operator/private ghp_SYNTHETICSECRET"
            )

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    log = io.StringIO()
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("codex", 100),)),
        worker=RaisingWorker(),
        worktree_root=tmp_path / "worktrees",
        workflow=WorkflowConfig(mode="multi-stage"),
        max_agent_failures=1,
        structured_logger=StructuredLogger(log),
    )
    task = Task.from_issue(Issue(number=1, title="exception"), worktree=str(worktree))
    task = task.transition(TaskState.DISPATCHED, current_agent="codex", now=NOW)
    scheduler.queue = scheduler.queue.append([task])
    scheduler._run_planning_and_review(task, "codex", NOW, "test-lease")
    assert scheduler.queue.get(1).status is TaskState.NEEDS_HUMAN
    cp = load_checkpoint(worktree, 1)
    assert cp is not None
    assert cp.failure_classification == "FAILURE"
    persisted = log.getvalue() + json.dumps(cp.to_dict())
    assert "synthetic exception detail" not in persisted
    assert "SYNTHETICSECRET" not in persisted
    assert "/home/operator/private" not in persisted
    assert "agent_finish" in log.getvalue()
