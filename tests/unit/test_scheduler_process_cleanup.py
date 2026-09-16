from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from subsched.models import AgentResult, AgentResultKind, Capacity, CapacityState, Issue, TaskState
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore


def test_process_cleanup_failure_escalates_without_retry(tmp_path: Path) -> None:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    worker = ScriptedWorker(
        {
            (308, "codex"): (
                AgentResult(
                    AgentResultKind.PROCESS_CLEANUP_FAILED,
                    output="codex cleanup failed",
                ),
            )
        }
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router((AgentConfig("codex", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        handoff_continuous=True,
        max_agent_failures=3,
    )
    scheduler.discover((Issue(number=308, title="Process cleanup safety"),))
    capacity = Capacity(
        agent="codex",
        state=CapacityState.AVAILABLE,
        reset_at=None,
        observed_at=now,
        source="provider",
        confidence="high",
    )

    assert scheduler.tick((capacity,), now=now) is True

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.attempt == 0
    assert task.per_agent_failures == ()
    assert task.needs_human_reason_code == "operator_decision_required"
    assert task.needs_human_reason == (
        "agent process cleanup failed; manual intervention required to inspect "
        "running processes before re-dispatching"
    )

    assert scheduler.tick((capacity,), now=now) is False
    assert worker.dispatches == [(308, "codex")]
