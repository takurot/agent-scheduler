"""Unit tests for `subsched.github.reconcile` (#277)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from subsched.github.reconcile import (
    PrLifecycleFetchKind,
    PrLifecycleState,
    ReconcileAction,
    WorktreePruneKind,
    fetch_pr_lifecycle_states,
    plan_reconciliation,
    prune_worktree_if_clean,
)
from subsched.models import Task, TaskState


def make_task(
    issue_number: int,
    status: TaskState = TaskState.READY_FOR_REVIEW,
    pr: int | None = 10,
    worktree: str | None = None,
) -> Task:
    return Task(
        task_id=f"github-{issue_number}",
        issue_number=issue_number,
        title=f"Task {issue_number}",
        labels=(),
        status=status,
        pr=pr,
        worktree=worktree,
    )


# --- fetch_pr_lifecycle_states -----------------------------------------------


def test_fetch_pr_lifecycle_states_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv[:3] == ["gh", "pr", "list"]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                '[{"number": 1, "state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"},'
                ' {"number": 2, "state": "CLOSED", "mergedAt": null},'
                ' {"number": 3, "state": "OPEN", "mergedAt": null}]'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo")

    assert result.kind is PrLifecycleFetchKind.SUCCESS
    assert result.states == {
        1: PrLifecycleState.MERGED,
        2: PrLifecycleState.CLOSED,
        3: PrLifecycleState.OPEN,
    }


def test_fetch_pr_lifecycle_states_gh_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not authenticated")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo")

    assert result.kind is PrLifecycleFetchKind.FAILURE
    assert result.states == {}
    assert "not authenticated" in result.error


def test_fetch_pr_lifecycle_states_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise OSError("gh not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo")

    assert result.kind is PrLifecycleFetchKind.FAILURE
    assert "invocation failed" in result.error


def test_fetch_pr_lifecycle_states_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=30.0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo")

    assert result.kind is PrLifecycleFetchKind.FAILURE


def test_fetch_pr_lifecycle_states_unparseable_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="not json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo")

    assert result.kind is PrLifecycleFetchKind.FAILURE
    assert "unparseable" in result.error


def test_fetch_pr_lifecycle_states_non_list_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout='{"not": "a list"}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo")

    assert result.kind is PrLifecycleFetchKind.FAILURE
    assert "invalid gh pr list output structure" in result.error


def test_fetch_pr_lifecycle_states_malformed_entry_not_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="[1, 2]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo")

    assert result.kind is PrLifecycleFetchKind.FAILURE
    assert "malformed entry" in result.error


def test_fetch_pr_lifecycle_states_malformed_entry_missing_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout='[{"number": "x"}]', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo")

    assert result.kind is PrLifecycleFetchKind.FAILURE
    assert "malformed entry" in result.error


def test_fetch_pr_lifecycle_states_unrecognized_state(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 0, stdout='[{"number": 5, "state": "DRAFT"}]', stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo")

    assert result.kind is PrLifecycleFetchKind.FAILURE
    assert "unrecognized PR state" in result.error


# --- plan_reconciliation -------------------------------------------------------


def test_plan_reconciliation_merged_advances_to_complete() -> None:
    task = make_task(1, pr=10)
    result = plan_reconciliation([task], {10: PrLifecycleState.MERGED})

    assert result.reconciled_complete == 1
    assert result.reconciled_needs_human == 0
    assert result.unchanged == 0
    assert result.updated_tasks[0].status is TaskState.COMPLETE
    assert result.items[0].action is ReconcileAction.COMPLETE


def test_plan_reconciliation_closed_escalates_to_needs_human() -> None:
    task = make_task(2, pr=20)
    result = plan_reconciliation([task], {20: PrLifecycleState.CLOSED})

    assert result.reconciled_needs_human == 1
    updated = result.updated_tasks[0]
    assert updated.status is TaskState.NEEDS_HUMAN
    assert updated.needs_human_reason == "PR #20 was closed without being merged"


def test_plan_reconciliation_open_remains_unchanged() -> None:
    task = make_task(3, pr=30)
    result = plan_reconciliation([task], {30: PrLifecycleState.OPEN})

    assert result.unchanged == 1
    assert result.updated_tasks[0].status is TaskState.READY_FOR_REVIEW
    assert result.items[0].reason == "PR is still open"


def test_plan_reconciliation_unknown_pr_state_fails_closed_to_unchanged() -> None:
    task = make_task(4, pr=40)
    result = plan_reconciliation([task], {})

    assert result.unchanged == 1
    assert result.updated_tasks[0].status is TaskState.READY_FOR_REVIEW
    assert "could not be determined" in result.items[0].reason


def test_plan_reconciliation_ignores_tasks_without_pr_or_wrong_status() -> None:
    no_pr = make_task(5, pr=None)
    wrong_status = make_task(6, status=TaskState.IN_PROGRESS, pr=60)
    result = plan_reconciliation([no_pr, wrong_status], {60: PrLifecycleState.MERGED})

    assert result.items == ()
    assert result.updated_tasks == (no_pr, wrong_status)


def test_plan_reconciliation_uses_provided_now() -> None:
    task = make_task(7, pr=70)
    fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
    result = plan_reconciliation([task], {70: PrLifecycleState.MERGED}, now=fixed_now)

    assert result.updated_tasks[0].updated_at == fixed_now


# --- prune_worktree_if_clean ---------------------------------------------------


def test_prune_worktree_if_clean_skips_symlink(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    target = tmp_path / "real"
    target.mkdir()
    link = worktree_root / "issue-1"
    link.symlink_to(target)

    result = prune_worktree_if_clean(tmp_path, worktree_root, 1, link)

    assert result.kind is WorktreePruneKind.SKIPPED
    assert "symlink" in result.reason


def test_prune_worktree_if_clean_skips_path_outside_root(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    result = prune_worktree_if_clean(tmp_path, worktree_root, 1, outside)

    assert result.kind is WorktreePruneKind.SKIPPED
    assert "does not match" in result.reason


def test_prune_worktree_if_clean_skips_missing_path(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    missing = worktree_root / "issue-1"

    result = prune_worktree_if_clean(tmp_path, worktree_root, 1, missing)

    assert result.kind is WorktreePruneKind.SKIPPED
    assert "does not exist" in result.reason


def test_prune_worktree_if_clean_fails_when_status_check_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    worktree_path = worktree_root / "issue-1"
    worktree_path.mkdir()

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = prune_worktree_if_clean(tmp_path, worktree_root, 1, worktree_path)

    assert result.kind is WorktreePruneKind.FAILED
    assert "could not check worktree status" in result.reason


def test_prune_worktree_if_clean_fails_when_git_status_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    worktree_path = worktree_root / "issue-1"
    worktree_path.mkdir()

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not a git repo")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = prune_worktree_if_clean(tmp_path, worktree_root, 1, worktree_path)

    assert result.kind is WorktreePruneKind.FAILED
    assert "git status failed" in result.reason


def test_prune_worktree_if_clean_skips_dirty_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    worktree_path = worktree_root / "issue-1"
    worktree_path.mkdir()

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=" M dirty.py\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = prune_worktree_if_clean(tmp_path, worktree_root, 1, worktree_path)

    assert result.kind is WorktreePruneKind.SKIPPED
    assert "uncommitted or untracked" in result.reason


def test_prune_worktree_if_clean_removes_clean_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    worktree_path = worktree_root / "issue-1"
    worktree_path.mkdir()

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["-C", str(worktree_path)]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = prune_worktree_if_clean(tmp_path, worktree_root, 1, worktree_path)

    assert result.kind is WorktreePruneKind.PRUNED
    assert any("worktree" in argv and "remove" in argv for argv in calls)


def test_prune_worktree_if_clean_fails_when_removal_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    worktree_path = worktree_root / "issue-1"
    worktree_path.mkdir()

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="worktree is locked")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = prune_worktree_if_clean(tmp_path, worktree_root, 1, worktree_path)

    assert result.kind is WorktreePruneKind.FAILED
    assert "git worktree remove failed" in result.reason


def test_prune_worktree_if_clean_fails_when_removal_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    worktree_path = worktree_root / "issue-1"
    worktree_path.mkdir()

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=30.0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = prune_worktree_if_clean(tmp_path, worktree_root, 1, worktree_path)

    assert result.kind is WorktreePruneKind.FAILED
    assert "could not remove worktree" in result.reason
