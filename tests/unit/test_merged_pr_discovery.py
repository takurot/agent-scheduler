from __future__ import annotations

from pathlib import Path

from subsched.github.pull_requests import MergedPrCheckKind, MergedPrCheckResult
from subsched.models import Issue, TaskState
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore


def _scheduler(tmp_path: Path, merged_pr_checker) -> Scheduler:
    return Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({}),
        worktree_root=tmp_path / "worktrees",
        merged_pr_checker=merged_pr_checker,
    )


def test_discover_without_checker_behaves_as_before(tmp_path: Path) -> None:
    """No merged_pr_checker configured (the default) must preserve pre-#146 behavior:
    every eligible issue becomes READY, no discovery_notes are produced."""
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({}),
        worktree_root=tmp_path / "worktrees",
    )
    scheduler.discover([Issue(number=101, title="Task 101")])

    assert scheduler.tasks[0].status is TaskState.READY
    assert scheduler.discovery_notes == ()


def test_discover_excludes_confirmed_merged_issue(tmp_path: Path) -> None:
    """Regression test for #146: a CONFIRMED merged PR must exclude the issue from
    discovery entirely -- no Task is created for it at all."""
    scheduler = _scheduler(
        tmp_path,
        lambda issue_number: MergedPrCheckResult(
            kind=MergedPrCheckKind.CONFIRMED, pr_number=131
        ),
    )
    scheduler.discover([Issue(number=129, title="Already merged")])

    assert scheduler.tasks == ()
    assert len(scheduler.discovery_notes) == 1
    note_issue, note_message = scheduler.discovery_notes[0]
    assert note_issue == 129
    assert "131" in note_message


def test_discover_flags_ambiguous_match_as_needs_human(tmp_path: Path) -> None:
    """Regression test for #146: an AMBIGUOUS match must fail closed -- the task is
    still created (so an operator can see and address it), but starts in NEEDS_HUMAN
    with the reason attached, not READY."""
    scheduler = _scheduler(
        tmp_path,
        lambda issue_number: MergedPrCheckResult(
            kind=MergedPrCheckKind.AMBIGUOUS,
            reason=f"issue #{issue_number} has an unconfirmed merged PR reference",
        ),
    )
    scheduler.discover([Issue(number=129, title="Ambiguous merge state")])

    assert len(scheduler.tasks) == 1
    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.needs_human_reason is not None
    assert "129" in task.needs_human_reason
    assert len(scheduler.discovery_notes) == 1


def test_discover_proceeds_as_ready_when_no_merged_pr_found(tmp_path: Path) -> None:
    scheduler = _scheduler(
        tmp_path, lambda issue_number: MergedPrCheckResult(kind=MergedPrCheckKind.NONE)
    )
    scheduler.discover([Issue(number=129, title="Never implemented")])

    assert len(scheduler.tasks) == 1
    assert scheduler.tasks[0].status is TaskState.READY
    assert scheduler.discovery_notes == ()


def test_discover_mixed_batch_handles_each_issue_independently(tmp_path: Path) -> None:
    def checker(issue_number: int) -> MergedPrCheckResult:
        if issue_number == 101:
            return MergedPrCheckResult(kind=MergedPrCheckKind.CONFIRMED, pr_number=201)
        if issue_number == 102:
            return MergedPrCheckResult(
                kind=MergedPrCheckKind.AMBIGUOUS, reason="issue #102 unconfirmed"
            )
        return MergedPrCheckResult(kind=MergedPrCheckKind.NONE)

    scheduler = _scheduler(tmp_path, checker)
    scheduler.discover(
        [
            Issue(number=101, title="Confirmed merged"),
            Issue(number=102, title="Ambiguous"),
            Issue(number=103, title="Not yet implemented"),
        ]
    )

    by_issue = {task.issue_number: task for task in scheduler.tasks}
    assert 101 not in by_issue
    assert by_issue[102].status is TaskState.NEEDS_HUMAN
    assert by_issue[103].status is TaskState.READY
    assert len(scheduler.discovery_notes) == 2
