from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from subsched.github import conflict as conflict_mod
from subsched.github import pull_requests as pr_mod
from subsched.github import push as push_mod
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
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore


def _available(agent: str) -> Capacity:
    return Capacity(
        agent=agent,
        state=CapacityState.AVAILABLE,
        observed_at=datetime.now(UTC),
        source="provider",
        confidence="high",
    )


def _scheduler(tmp_path: Path, *, push_enabled: bool, ci_checker=None) -> Scheduler:
    return Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)}),
        worktree_root=tmp_path / "worktrees",
        push_enabled=push_enabled,
        repo="owner/repo",
        base_branch="main",
        ci_checker=ci_checker,
    )


def test_push_disabled_completes_without_any_git_or_github_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (push_enabled=False) behavior must be unchanged: no git/gh calls at all."""

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("git/gh must not be called when push_enabled is False")

    monkeypatch.setattr(conflict_mod, "rebase_onto_base", boom)
    monkeypatch.setattr(push_mod, "push_task_branch", boom)
    monkeypatch.setattr(pr_mod, "create_or_get_pull_request", boom)

    scheduler = _scheduler(tmp_path, push_enabled=False)
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude")])

    task = scheduler.tasks[0]
    assert task.status is TaskState.COMPLETE
    assert task.pr is None


def test_push_enabled_happy_path_reaches_ready_for_review_with_pr_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for #142: opening a PR must stop at READY_FOR_REVIEW, not jump
    straight to COMPLETE -- CI hasn't even been checked yet at this point, and a human
    hasn't reviewed anything. COMPLETE previously happened unconditionally in the same
    call, so "Task Completion Rate" was always 100% the moment a PR was opened."""
    monkeypatch.setattr(
        conflict_mod,
        "rebase_onto_base",
        lambda worktree_dir, base_branch="main", **kw: conflict_mod.RebaseResult(
            status=conflict_mod.RebaseStatus.SUCCESS
        ),
    )
    monkeypatch.setattr(
        push_mod,
        "push_task_branch",
        lambda worktree_dir, branch_name, **kw: push_mod.PushResult(
            kind=push_mod.PushResultKind.SUCCESS, output="", branch=branch_name
        ),
    )
    monkeypatch.setattr(
        pr_mod,
        "create_or_get_pull_request",
        lambda task, branch_name, **kw: pr_mod.PullRequestResult(
            kind=pr_mod.PullRequestResultKind.SUCCESS,
            info=pr_mod.PullRequestInfo(
                number=42, url="https://example.invalid/pull/42", title="t", body="b"
            ),
        ),
    )

    scheduler = _scheduler(tmp_path, push_enabled=True)
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude")])

    task = scheduler.tasks[0]
    assert task.status is TaskState.READY_FOR_REVIEW
    assert task.pr == 42


def test_rebase_conflict_escalates_to_needs_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        conflict_mod,
        "rebase_onto_base",
        lambda worktree_dir, base_branch="main", **kw: conflict_mod.RebaseResult(
            status=conflict_mod.RebaseStatus.CONFLICT, conflicted_files=("a.py",)
        ),
    )

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("push must not be attempted after a rebase conflict")

    monkeypatch.setattr(push_mod, "push_task_branch", boom)

    scheduler = _scheduler(tmp_path, push_enabled=True)
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude")])

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.pr is None
    # Regression test for #128: the escalation reason must be persisted, not discarded.
    assert task.needs_human_reason is not None
    assert "conflict" in task.needs_human_reason.casefold()


def test_push_failure_escalates_to_needs_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        conflict_mod,
        "rebase_onto_base",
        lambda worktree_dir, base_branch="main", **kw: conflict_mod.RebaseResult(
            status=conflict_mod.RebaseStatus.SUCCESS
        ),
    )
    monkeypatch.setattr(
        push_mod,
        "push_task_branch",
        lambda worktree_dir, branch_name, **kw: push_mod.PushResult(
            kind=push_mod.PushResultKind.PERMISSION_DENIED, output="denied", branch=branch_name
        ),
    )

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("PR creation must not be attempted after a failed push")

    monkeypatch.setattr(pr_mod, "create_or_get_pull_request", boom)

    scheduler = _scheduler(tmp_path, push_enabled=True)
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude")])

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.pr is None
    # Regression test for #128: the push failure reason must be persisted, not discarded.
    assert task.needs_human_reason is not None
    assert "denied" in task.needs_human_reason


def test_pr_creation_failure_escalates_to_needs_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        conflict_mod,
        "rebase_onto_base",
        lambda worktree_dir, base_branch="main", **kw: conflict_mod.RebaseResult(
            status=conflict_mod.RebaseStatus.SUCCESS
        ),
    )
    monkeypatch.setattr(
        push_mod,
        "push_task_branch",
        lambda worktree_dir, branch_name, **kw: push_mod.PushResult(
            kind=push_mod.PushResultKind.SUCCESS, output="", branch=branch_name
        ),
    )
    monkeypatch.setattr(
        pr_mod,
        "create_or_get_pull_request",
        lambda task, branch_name, **kw: pr_mod.PullRequestResult(
            kind=pr_mod.PullRequestResultKind.FAILURE,
            info=None,
            output="gh: pull request create failed: already exists",
        ),
    )

    scheduler = _scheduler(tmp_path, push_enabled=True)
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude")])

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.pr is None
    # Regression test for #128: the PR-creation failure reason must be persisted.
    assert task.needs_human_reason is not None
    assert "already exists" in task.needs_human_reason


def _ready_for_review_task(issue_number: int, pr: int) -> Task:
    return replace(
        Task.from_issue(Issue(number=issue_number, title="Task")),
        status=TaskState.READY_FOR_REVIEW,
        pr=pr,
    )


def test_ci_monitoring_disabled_by_default_leaves_ready_for_review_unchanged(
    tmp_path: Path,
) -> None:
    """Regression test for #142: without CI monitoring configured (the default),
    READY_FOR_REVIEW must not silently become COMPLETE just because tick() ran."""
    scheduler = _scheduler(tmp_path, push_enabled=True)
    task = _ready_for_review_task(101, pr=7)
    scheduler.discover([])
    scheduler.queue = scheduler.queue.append([task])
    scheduler.tick([])

    assert scheduler.tasks[0].status is TaskState.READY_FOR_REVIEW


def test_ci_monitoring_promotes_to_complete_on_pass(tmp_path: Path) -> None:
    from subsched.github.checks import CICheckState, PRChecksStatus

    scheduler = _scheduler(
        tmp_path,
        push_enabled=True,
        ci_checker=lambda pr: PRChecksStatus(
            pr_number=pr, overall_state=CICheckState.PASS, checks=()
        ),
    )
    task = _ready_for_review_task(101, pr=7)
    scheduler.discover([])
    scheduler.queue = scheduler.queue.append([task])
    scheduler.tick([])

    assert scheduler.tasks[0].status is TaskState.COMPLETE


def test_ci_monitoring_escalates_to_needs_human_on_fail(tmp_path: Path) -> None:
    from subsched.github.checks import CICheckResult, CICheckState, PRChecksStatus

    scheduler = _scheduler(
        tmp_path,
        push_enabled=True,
        ci_checker=lambda pr: PRChecksStatus(
            pr_number=pr,
            overall_state=CICheckState.FAIL,
            checks=(
                CICheckResult(
                    name="test", state=CICheckState.FAIL, description="failed", link=""
                ),
            ),
        ),
    )
    task = _ready_for_review_task(101, pr=7)
    scheduler.discover([])
    scheduler.queue = scheduler.queue.append([task])
    scheduler.tick([])

    updated = scheduler.tasks[0]
    assert updated.status is TaskState.NEEDS_HUMAN
    assert updated.needs_human_reason is not None
    assert "7" in updated.needs_human_reason
    assert "test" in updated.needs_human_reason


def test_ci_monitoring_leaves_pending_unchanged(tmp_path: Path) -> None:
    from subsched.github.checks import CICheckState, PRChecksStatus

    scheduler = _scheduler(
        tmp_path,
        push_enabled=True,
        ci_checker=lambda pr: PRChecksStatus(
            pr_number=pr, overall_state=CICheckState.PENDING, checks=()
        ),
    )
    task = _ready_for_review_task(101, pr=7)
    scheduler.discover([])
    scheduler.queue = scheduler.queue.append([task])
    scheduler.tick([])

    assert scheduler.tasks[0].status is TaskState.READY_FOR_REVIEW


def test_ci_monitoring_ignores_tasks_without_pr(tmp_path: Path) -> None:
    """A READY_FOR_REVIEW task with no pr number (shouldn't normally happen, but must
    not crash the CI-polling step) is simply skipped."""

    def boom(pr: int) -> object:
        raise AssertionError("ci_checker must not be called for a task with no pr number")

    scheduler = _scheduler(tmp_path, push_enabled=True, ci_checker=boom)
    task = replace(
        Task.from_issue(Issue(number=101, title="Task")), status=TaskState.READY_FOR_REVIEW
    )
    scheduler.discover([])
    scheduler.queue = scheduler.queue.append([task])
    scheduler.tick([])

    assert scheduler.tasks[0].status is TaskState.READY_FOR_REVIEW
