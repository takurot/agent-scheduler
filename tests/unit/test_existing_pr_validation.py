from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from subsched.github.pull_requests import (
    ExistingPrCheckKind,
    PullRequestResultKind,
    create_or_get_pull_request,
    lookup_existing_pr,
)
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


def test_lookup_existing_pr_confirmed_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR matching head branch, base branch, and exact task marker is CONFIRMED and reused."""
    fake_json = json.dumps([
        {
            "number": 42,
            "url": "https://github.com/takurot/agent-scheduler/pull/42",
            "title": "Fix bug (#188)",
            "body": "Implements work for #188.\n\n## Summary\n...",
            "headRefName": "subsched/issue-188",
            "baseRefName": "main",
        }
    ])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 0, stdout=fake_json, stderr=""),
    )
    result = lookup_existing_pr(
        branch_name="subsched/issue-188",
        issue_number=188,
        base="main",
    )
    assert result.kind is ExistingPrCheckKind.CONFIRMED
    assert result.info is not None
    assert result.info.number == 42

    task = Task.from_issue(Issue(number=188, title="Fix bug"))
    pr_result = create_or_get_pull_request(task, "subsched/issue-188", base="main")
    assert pr_result.kind is PullRequestResultKind.SUCCESS
    assert pr_result.info is not None
    assert pr_result.info.number == 42


def test_existing_pr_wrong_base_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR with matching head branch but wrong base branch must be rejected as AMBIGUOUS."""
    fake_json = json.dumps([
        {
            "number": 43,
            "url": "https://github.com/takurot/agent-scheduler/pull/43",
            "title": "Fix bug (#188)",
            "body": "Implements work for #188.\n\n## Summary\n...",
            "headRefName": "subsched/issue-188",
            "baseRefName": "develop",
        }
    ])
    run_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        run_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=fake_json, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = lookup_existing_pr(
        branch_name="subsched/issue-188",
        issue_number=188,
        base="main",
    )
    assert result.kind is ExistingPrCheckKind.AMBIGUOUS
    assert "base" in result.reason.lower()

    task = Task.from_issue(Issue(number=188, title="Fix bug"))
    pr_result = create_or_get_pull_request(task, "subsched/issue-188", base="main")
    assert pr_result.kind is PullRequestResultKind.FAILURE
    assert pr_result.info is None
    # Must fail closed without calling `gh pr create`
    assert len(run_calls) == 2
    assert not any("create" in argv for argv in run_calls)


def test_existing_pr_unrelated_body_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR with matching branch but unrelated body/marker must be rejected as AMBIGUOUS."""
    fake_json = json.dumps([
        {
            "number": 44,
            "url": "https://github.com/takurot/agent-scheduler/pull/44",
            "title": "Manual PR",
            "body": "This is a manual change without scheduler marker",
            "headRefName": "subsched/issue-188",
            "baseRefName": "main",
        }
    ])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 0, stdout=fake_json, stderr=""),
    )
    result = lookup_existing_pr(
        branch_name="subsched/issue-188",
        issue_number=188,
        base="main",
    )
    assert result.kind is ExistingPrCheckKind.AMBIGUOUS
    assert "prefix" in result.reason.lower() or "marker" in result.reason.lower()

    task = Task.from_issue(Issue(number=188, title="Fix bug"))
    pr_result = create_or_get_pull_request(task, "subsched/issue-188", base="main")
    assert pr_result.kind is PullRequestResultKind.FAILURE
    assert pr_result.info is None


def test_existing_pr_multiple_matches_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple open PRs returned by gh must fail closed as AMBIGUOUS."""
    fake_json = json.dumps([
        {
            "number": 45,
            "url": "https://github.com/takurot/agent-scheduler/pull/45",
            "title": "PR 1 (#188)",
            "body": "Implements work for #188.\n\n## Summary\n...",
            "headRefName": "subsched/issue-188",
            "baseRefName": "main",
        },
        {
            "number": 46,
            "url": "https://github.com/takurot/agent-scheduler/pull/46",
            "title": "PR 2 (#188)",
            "body": "Implements work for #188.\n\n## Summary\n...",
            "headRefName": "subsched/issue-188",
            "baseRefName": "main",
        },
    ])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 0, stdout=fake_json, stderr=""),
    )
    result = lookup_existing_pr(
        branch_name="subsched/issue-188",
        issue_number=188,
        base="main",
    )
    assert result.kind is ExistingPrCheckKind.AMBIGUOUS
    assert "multiple" in result.reason.lower()

    task = Task.from_issue(Issue(number=188, title="Fix bug"))
    pr_result = create_or_get_pull_request(task, "subsched/issue-188", base="main")
    assert pr_result.kind is PullRequestResultKind.FAILURE
    assert pr_result.info is None


def test_existing_pr_malformed_output_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed or nonzero gh output fails closed as AMBIGUOUS."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="gh error"),
    )
    result = lookup_existing_pr(
        branch_name="subsched/issue-188",
        issue_number=188,
        base="main",
    )
    assert result.kind is ExistingPrCheckKind.AMBIGUOUS

    task = Task.from_issue(Issue(number=188, title="Fix bug"))
    pr_result = create_or_get_pull_request(task, "subsched/issue-188", base="main")
    assert pr_result.kind is PullRequestResultKind.FAILURE
    assert pr_result.info is None


def test_existing_pr_head_ref_mismatch_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR with partial prefix match on head branch must be rejected."""
    fake_json = json.dumps([
        {
            "number": 47,
            "url": "https://github.com/takurot/agent-scheduler/pull/47",
            "title": "PR (#188)",
            "body": "Implements work for #188.\n\n## Summary\n...",
            "headRefName": "subsched/issue-188-extra",
            "baseRefName": "main",
        }
    ])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 0, stdout=fake_json, stderr=""),
    )
    result = lookup_existing_pr(
        branch_name="subsched/issue-188",
        issue_number=188,
        base="main",
    )
    assert result.kind is ExistingPrCheckKind.AMBIGUOUS
    assert "headrefname" in result.reason.lower() or "head" in result.reason.lower()


def test_scheduler_escalates_to_needs_human_when_existing_pr_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a task reaches VERIFYING with push_enabled=True, but existing PR lookup
    detects an ambiguous or mismatching PR, the task transitions to NEEDS_HUMAN."""
    from subsched.github import push as push_mod
    from subsched.github.push import PushResult, PushResultKind

    # Mock push to succeed
    monkeypatch.setattr(
        push_mod,
        "push_task_branch",
        lambda worktree_dir, branch_name, **kw: PushResult(
            kind=PushResultKind.SUCCESS, output="", branch=branch_name
        ),
    )

    # Mock gh pr list to return an existing PR with wrong base
    fake_json = json.dumps([
        {
            "number": 99,
            "url": "https://github.com/takurot/agent-scheduler/pull/99",
            "title": "Wrong base (#188)",
            "body": "Implements work for #188.\n\n## Summary\n...",
            "headRefName": "subsched/issue-188",
            "baseRefName": "release",
        }
    ])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 0, stdout=fake_json, stderr=""),
    )

    store = JsonStateStore(tmp_path / "state.json")
    task = Task.from_issue(Issue(number=188, title="Validate PR"))
    store.save_tasks((task,))

    scheduler = Scheduler(
        store=store,
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({(188, "claude"): (AgentResult(AgentResultKind.PASS),)}),
        worktree_root=tmp_path / "worktrees",
        push_enabled=True,
        repo="owner/repo",
        base_branch="main",
    )

    # 1st tick: READY -> IN_PROGRESS -> VERIFYING (worker passes) -> finalize -> NEEDS_HUMAN
    cap = Capacity("claude", CapacityState.AVAILABLE, datetime.now(UTC), "provider", "high")
    scheduler.tick([cap])

    final_task = scheduler.tasks[0]
    assert final_task.status is TaskState.NEEDS_HUMAN
    assert final_task.needs_human_reason is not None
    assert "PR creation failed" in final_task.needs_human_reason
    assert "targets base branch 'release', expected 'main'" in final_task.needs_human_reason
