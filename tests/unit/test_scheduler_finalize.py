from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from subsched.github import conflict as conflict_mod
from subsched.github import pull_requests as pr_mod
from subsched.github import push as push_mod
from subsched.models import AgentResult, AgentResultKind, Capacity, CapacityState, Issue, TaskState
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


def _scheduler(tmp_path: Path, *, push_enabled: bool) -> Scheduler:
    return Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)}),
        worktree_root=tmp_path / "worktrees",
        push_enabled=push_enabled,
        repo="owner/repo",
        base_branch="main",
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


def test_push_enabled_happy_path_completes_with_pr_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        conflict_mod,
        "rebase_onto_base",
        lambda worktree_dir, base_branch="main", **kw: conflict_mod.RebaseResult(
            status=conflict_mod.RebaseStatus.SUCCESS
        ),
    )
    monkeypatch.setattr(pr_mod, "find_close_keyword_commits", lambda worktree_dir, base, **kw: ())
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
    assert task.status is TaskState.COMPLETE
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
    monkeypatch.setattr(pr_mod, "find_close_keyword_commits", lambda worktree_dir, base, **kw: ())
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
    monkeypatch.setattr(pr_mod, "find_close_keyword_commits", lambda worktree_dir, base, **kw: ())
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


def test_create_pr_disabled_completes_without_any_git_or_github_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for #136: github.completion.create_pr=false must be honored --
    previously push_enabled alone controlled both push and PR creation, so create_pr=false
    in config had no runtime effect at all and PR adapter was still called."""

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("git/gh must not be called when create_pr_enabled is False")

    monkeypatch.setattr(conflict_mod, "rebase_onto_base", boom)
    monkeypatch.setattr(push_mod, "push_task_branch", boom)
    monkeypatch.setattr(pr_mod, "create_or_get_pull_request", boom)

    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)}),
        worktree_root=tmp_path / "worktrees",
        push_enabled=True,
        create_pr_enabled=False,
        repo="owner/repo",
        base_branch="main",
    )
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude")])

    task = scheduler.tasks[0]
    assert task.status is TaskState.COMPLETE
    assert task.pr is None


def test_create_pr_enabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The safe default (create_pr_enabled unset) must preserve the existing PR-creation
    behavior -- this is a regression guard for the #136 wiring, not a new default."""
    monkeypatch.setattr(
        conflict_mod,
        "rebase_onto_base",
        lambda worktree_dir, base_branch="main", **kw: conflict_mod.RebaseResult(
            status=conflict_mod.RebaseStatus.SUCCESS
        ),
    )
    monkeypatch.setattr(pr_mod, "find_close_keyword_commits", lambda worktree_dir, base, **kw: ())
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
                number=7, url="https://example.invalid/pull/7", title="t", body="b"
            ),
        ),
    )

    scheduler = _scheduler(tmp_path, push_enabled=True)
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude")])

    task = scheduler.tasks[0]
    assert task.status is TaskState.COMPLETE
    assert task.pr == 7


def test_commit_message_close_keyword_escalates_to_needs_human_before_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for #140: a worker's local commit message containing a GitHub
    auto-close keyword (e.g. "Closes #130", as actually happened in PR #135's dogfood
    commit) must block push/PR entirely, not just get sanitized in the PR body."""
    monkeypatch.setattr(
        conflict_mod,
        "rebase_onto_base",
        lambda worktree_dir, base_branch="main", **kw: conflict_mod.RebaseResult(
            status=conflict_mod.RebaseStatus.SUCCESS
        ),
    )
    monkeypatch.setattr(
        pr_mod,
        "find_close_keyword_commits",
        lambda worktree_dir, base, **kw: (
            pr_mod.CloseKeywordViolation(commit="abc123def456", keyword_context="Closes #130"),
        ),
    )

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("push/PR must not be attempted when a commit has a close keyword")

    monkeypatch.setattr(push_mod, "push_task_branch", boom)
    monkeypatch.setattr(pr_mod, "create_or_get_pull_request", boom)

    scheduler = _scheduler(tmp_path, push_enabled=True)
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude")])

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.pr is None
    assert task.needs_human_reason is not None
    assert "abc123def456" in task.needs_human_reason
    assert "Closes #130" in task.needs_human_reason


def test_commit_message_check_failure_fails_closed_before_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the commit-message check itself can't be completed (e.g. git log failed),
    the task must fail closed to NEEDS_HUMAN rather than proceeding to push anyway."""
    monkeypatch.setattr(
        conflict_mod,
        "rebase_onto_base",
        lambda worktree_dir, base_branch="main", **kw: conflict_mod.RebaseResult(
            status=conflict_mod.RebaseStatus.SUCCESS
        ),
    )
    monkeypatch.setattr(pr_mod, "find_close_keyword_commits", lambda worktree_dir, base, **kw: None)

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("push/PR must not be attempted when the check is inconclusive")

    monkeypatch.setattr(push_mod, "push_task_branch", boom)
    monkeypatch.setattr(pr_mod, "create_or_get_pull_request", boom)

    scheduler = _scheduler(tmp_path, push_enabled=True)
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude")])

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.needs_human_reason is not None
    assert "could not verify" in task.needs_human_reason
