from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.checkpoint import MechanicalCheckpoint, save_checkpoint
from subsched.models import AgentResult, AgentResultKind, Capacity, CapacityState, Issue, TaskState
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler
from subsched.storage import JsonStateStore


def _available(agent: str, *, observed_at: datetime) -> Capacity:
    return Capacity(
        agent=agent,
        state=CapacityState.AVAILABLE,
        observed_at=observed_at,
        source="provider",
        confidence="high",
    )


def _write_handoff(worktree_dir: Path, issue_number: int, *, title: str, timestamp: str) -> None:
    handoffs_dir = worktree_dir / ".ai" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    (handoffs_dir / f"{issue_number}.md").write_text(
        f"""# Issue

#{issue_number} {title}

## Goal

{title}

## Current Plan

- Plan

## Completed

- Did real work

## Current Work

Wrapping up

## Decisions

- Used approach X

## Known Broken State

- None

## Next Action

- Verify

## Timestamp

{timestamp}
""",
        encoding="utf-8",
    )


class _ScriptedHandoffWorker:
    """Like ScriptedWorker, but optionally updates (or reads back) the handoff file
    during run(), so tests can simulate an Agent that does/doesn't follow the
    continuous-handoff contract."""

    def __init__(
        self,
        scripts: dict[tuple[int, str], tuple[AgentResult, ...]],
        *,
        update_handoff_at: datetime | None = None,
        write_checkpoint_at: datetime | None = None,
        record_handoff_reads: list[str] | None = None,
    ) -> None:
        self._scripts = {key: deque(results) for key, results in scripts.items()}
        self.update_handoff_at = update_handoff_at
        self.write_checkpoint_at = write_checkpoint_at
        self.record_handoff_reads = record_handoff_reads

    def run(self, task, agent):  # type: ignore[no-untyped-def]
        assert task.worktree is not None
        worktree_dir = Path(task.worktree)
        if self.record_handoff_reads is not None:
            handoff_path = worktree_dir / ".ai" / "handoffs" / f"{task.issue_number}.md"
            self.record_handoff_reads.append(handoff_path.read_text(encoding="utf-8"))
        if self.update_handoff_at is not None:
            _write_handoff(
                worktree_dir,
                task.issue_number,
                title=task.title,
                timestamp=self.update_handoff_at.isoformat(),
            )
        if self.write_checkpoint_at is not None:
            save_checkpoint(
                worktree_dir,
                MechanicalCheckpoint(
                    issue_number=task.issue_number,
                    git_status="",
                    git_diff_stat="",
                    changed_files=(),
                    head_commit="abc123",
                    test_results="",
                    last_agent_output="",
                    exit_code=0,
                    failure_classification="PASS",
                    timestamp=self.write_checkpoint_at.isoformat(),
                ),
            )
        return self._scripts[(task.issue_number, agent)].popleft()


def _scheduler(tmp_path: Path, worker: object, *, handoff_continuous: bool) -> Scheduler:
    return Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router([AgentConfig("claude", priority=100)]),
        worker=worker,  # type: ignore[arg-type]
        worktree_root=tmp_path / "worktrees",
        handoff_continuous=handoff_continuous,
        max_agent_failures=100,
    )


def test_handoff_continuous_disabled_by_default_no_enforcement(tmp_path: Path) -> None:
    """Backward-compat regression test: handoff_continuous defaults to False, so a
    stale/never-updated handoff must not change behavior for any existing caller."""
    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    worker = _ScriptedHandoffWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = _scheduler(tmp_path, worker, handoff_continuous=False)
    scheduler.discover([Issue(number=101, title="Task 101")])

    scheduler.tick([_available("claude", observed_at=far_future)], now=far_future)

    task = scheduler.tasks[0]
    assert task.status is not TaskState.NEEDS_HUMAN
    assert task.status is TaskState.COMPLETE


def test_handoff_continuous_escalates_on_stale_handoff_after_pass(tmp_path: Path) -> None:
    """Regression test for #145 (the dogfooded #130 scenario): the Agent reports PASS
    but never advanced the handoff timestamp past dispatch -- with handoff_continuous
    enabled, this must escalate to NEEDS_HUMAN instead of being trusted."""
    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    worker = _ScriptedHandoffWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)})
    scheduler = _scheduler(tmp_path, worker, handoff_continuous=True)
    scheduler.discover([Issue(number=101, title="Task 101")])

    scheduler.tick([_available("claude", observed_at=far_future)], now=far_future)

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.needs_human_reason is not None
    assert "did not advance" in task.needs_human_reason


def test_handoff_continuous_escalates_on_stale_handoff_after_failure(tmp_path: Path) -> None:
    """The freshness check runs unconditionally after worker.run() returns, regardless
    of outcome kind -- a FAILURE result with a stale handoff must also escalate."""
    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    worker = _ScriptedHandoffWorker({(101, "claude"): (AgentResult(AgentResultKind.FAILURE),)})
    scheduler = _scheduler(tmp_path, worker, handoff_continuous=True)
    scheduler.discover([Issue(number=101, title="Task 101")])

    scheduler.tick([_available("claude", observed_at=far_future)], now=far_future)

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN


def test_handoff_continuous_proceeds_normally_with_freshly_updated_handoff(
    tmp_path: Path,
) -> None:
    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    worker = _ScriptedHandoffWorker(
        {(101, "claude"): (AgentResult(AgentResultKind.PASS),)},
        update_handoff_at=far_future + timedelta(minutes=1),
    )
    scheduler = _scheduler(tmp_path, worker, handoff_continuous=True)
    scheduler.discover([Issue(number=101, title="Task 101")])

    scheduler.tick([_available("claude", observed_at=far_future)], now=far_future)

    task = scheduler.tasks[0]
    assert task.status is TaskState.COMPLETE


def test_handoff_continuous_does_not_escalate_capacity_session_on_stale_handoff(
    tmp_path: Path,
) -> None:
    """Regression test for #145 code review: a capacity cutoff is an externally-imposed
    interruption, not evidence the Agent ignored the handoff contract -- it may have had
    no opportunity to write anything before capacity ran out. A stale handoff must not
    force this straight to NEEDS_HUMAN, bypassing the normal capacity-driven
    agent-switch/retry design (cooldown tracking, requeue_after_capacity_event)."""
    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    reset = far_future + timedelta(hours=1)
    worker = _ScriptedHandoffWorker(
        {
            (101, "claude"): (
                AgentResult(AgentResultKind.CAPACITY_SESSION, reset_at=reset, output="session"),
            )
        }
    )
    scheduler = _scheduler(tmp_path, worker, handoff_continuous=True)
    scheduler.discover([Issue(number=101, title="Task 101")])

    scheduler.tick([_available("claude", observed_at=far_future)], now=far_future)

    task = scheduler.tasks[0]
    assert task.status is not TaskState.NEEDS_HUMAN
    # The existing capacity-event bookkeeping in _handle_result must still have run.
    assert task.capacity_events == 1


def test_handoff_continuous_does_not_escalate_timeout_on_stale_handoff(tmp_path: Path) -> None:
    """A TIMEOUT result before any handoff update must consume a normal retry attempt
    (max_agent_failures), not force an immediate human escalation on first occurrence."""
    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    worker = _ScriptedHandoffWorker({(101, "claude"): (AgentResult(AgentResultKind.TIMEOUT),)})
    scheduler = _scheduler(tmp_path, worker, handoff_continuous=True)
    scheduler.discover([Issue(number=101, title="Task 101")])

    scheduler.tick([_available("claude", observed_at=far_future)], now=far_future)

    task = scheduler.tasks[0]
    assert task.status is not TaskState.NEEDS_HUMAN
    assert task.status is TaskState.READY  # normal retry, not escalation


def test_handoff_continuous_recovers_via_fresh_mechanical_checkpoint(tmp_path: Path) -> None:
    """#145: a stale/invalid handoff is still allowed to continue if a mechanical
    checkpoint proves the same or newer progress happened, instead of escalating."""
    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    worker = _ScriptedHandoffWorker(
        {(101, "claude"): (AgentResult(AgentResultKind.PASS),)},
        write_checkpoint_at=far_future + timedelta(minutes=1),
    )
    scheduler = _scheduler(tmp_path, worker, handoff_continuous=True)
    scheduler.discover([Issue(number=101, title="Task 101")])

    scheduler.tick([_available("claude", observed_at=far_future)], now=far_future)

    task = scheduler.tasks[0]
    assert task.status is not TaskState.NEEDS_HUMAN
    assert task.status is TaskState.COMPLETE


def test_failover_reads_previously_updated_handoff_not_the_bootstrap_placeholder(
    tmp_path: Path,
) -> None:
    """Regression test for #145's failover acceptance criterion: an Agent that properly
    updated the handoff before a transient FAILURE must have that content still present
    -- not reset to the bootstrap placeholder -- when the task is retried."""
    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    reads: list[str] = []
    first_attempt = _ScriptedHandoffWorker(
        {(101, "claude"): (AgentResult(AgentResultKind.FAILURE),)},
        update_handoff_at=far_future + timedelta(minutes=1),
        record_handoff_reads=reads,
    )
    # handoff_continuous off for this test so the FAILURE outcome retries normally
    # instead of being escalated by the freshness gate -- isolating the failover
    # behavior (bootstrap_task_files never overwriting an existing handoff) under test.
    scheduler = _scheduler(tmp_path, first_attempt, handoff_continuous=False)
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude", observed_at=far_future)], now=far_future)
    assert scheduler.tasks[0].status is TaskState.READY

    later = far_future + timedelta(minutes=5)
    second_attempt = _ScriptedHandoffWorker(
        {(101, "claude"): (AgentResult(AgentResultKind.PASS),)}, record_handoff_reads=reads
    )
    scheduler.worker = second_attempt  # type: ignore[assignment]
    scheduler.tick([_available("claude", observed_at=later)], now=later)

    assert scheduler.tasks[0].status is TaskState.COMPLETE
    assert len(reads) == 2
    # The second attempt must see the first attempt's real content, not the bootstrap
    # placeholder ("Task bootstrapped" / "Initial implementation").
    assert "Did real work" in reads[1]
    assert "Task bootstrapped" not in reads[1]
