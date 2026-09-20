"""Unit tests for `subsched.github.reconcile` (#277)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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


def _view_payload(argv: list[str], state: str = "OPEN") -> str:
    number = int(argv[3])
    return f'{{"number": {number}, "state": "{state}"}}'


def test_fetch_pr_lifecycle_states_queries_each_tracked_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#377: every tracked PR is fetched individually, so PRs older than any list
    window (e.g. #1 among 101..200) are still resolved."""
    calls: list[list[str]] = []
    states = {1: "MERGED", 2: "CLOSED", 500: "OPEN"}

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert argv[:3] == ["gh", "pr", "view"]
        assert argv[argv.index("--repo") + 1] == "owner/repo"
        return subprocess.CompletedProcess(
            argv, 0, stdout=_view_payload(argv, states[int(argv[3])]), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo", [500, 1, 2, 1])

    assert result.kind is PrLifecycleFetchKind.SUCCESS
    assert result.states == {
        1: PrLifecycleState.MERGED,
        2: PrLifecycleState.CLOSED,
        500: PrLifecycleState.OPEN,
    }
    # Deduplicated, deterministic ascending order, one call per unique PR.
    assert [c[3] for c in calls] == ["1", "2", "500"]


def test_fetch_pr_lifecycle_states_bounds_api_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=_view_payload(argv), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo", range(1, 11), max_requests=3)

    assert result.kind is PrLifecycleFetchKind.SUCCESS
    assert len(calls) == 3
    # PRs beyond the cap are simply absent (unknown), never guessed.
    assert set(result.states) == {1, 2, 3}


def test_fetch_pr_lifecycle_states_no_prs_makes_no_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("gh must not be called without tracked PRs")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo", [])

    assert result.kind is PrLifecycleFetchKind.SUCCESS
    assert result.states == {}


def test_fetch_pr_lifecycle_states_gh_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not authenticated")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo", [1])

    assert result.kind is PrLifecycleFetchKind.FAILURE
    assert result.states == {}
    assert "not authenticated" in result.error


def test_fetch_pr_lifecycle_states_partial_failure_discards_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure on any tracked PR fails the whole fetch so no state is mutated."""

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if argv[3] == "2":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not found")
        return subprocess.CompletedProcess(argv, 0, stdout=_view_payload(argv), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo", [1, 2, 3])

    assert result.kind is PrLifecycleFetchKind.FAILURE
    assert result.states == {}
    assert "#2" in result.error


def test_fetch_pr_lifecycle_states_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise OSError("gh not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo", [1])

    assert result.kind is PrLifecycleFetchKind.FAILURE
    assert "invocation failed" in result.error


def test_fetch_pr_lifecycle_states_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=30.0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo", [1])

    assert result.kind is PrLifecycleFetchKind.FAILURE


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("not json", "unparseable"),
        ("[1, 2]", "invalid gh pr view output structure"),
        ('{"number": "x", "state": "OPEN"}', "malformed"),
        ('{"number": 1}', "malformed"),
        ('{"number": 2, "state": "OPEN"}', "does not match"),
        ('{"number": 1, "state": "DRAFT"}', "unrecognized PR state"),
    ],
)
def test_fetch_pr_lifecycle_states_rejects_bad_payloads(
    monkeypatch: pytest.MonkeyPatch, stdout: str, expected: str
) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = fetch_pr_lifecycle_states("owner/repo", [1])

    assert result.kind is PrLifecycleFetchKind.FAILURE
    assert result.states == {}
    assert expected in result.error


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


def test_prune_worktree_if_clean_skips_tracked_change_under_ai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    worktree_path = worktree_root / "issue-1"
    worktree_path.mkdir()

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 0, stdout=" M .ai/tasks/1.md\0", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = prune_worktree_if_clean(tmp_path, worktree_root, 1, worktree_path)

    assert result.kind is WorktreePruneKind.SKIPPED


def test_prune_worktree_if_clean_skips_untracked_file_outside_ai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    worktree_path = worktree_root / "issue-1"
    worktree_path.mkdir()

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="?? notes.txt\0", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = prune_worktree_if_clean(tmp_path, worktree_root, 1, worktree_path)

    assert result.kind is WorktreePruneKind.SKIPPED


def test_prune_worktree_if_clean_removes_worktree_with_only_untracked_ai_files(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    worktree_root = repo_root / ".ai" / "worktrees"
    worktree_path = worktree_root / "issue-1"
    repo_root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo_root)], check=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    (repo_root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "--quiet", "-m", "initial"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "add", "--quiet", str(worktree_path)],
        check=True,
    )
    task_path = worktree_path / ".ai" / "tasks" / "1.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("scheduler state\n", encoding="utf-8")

    result = prune_worktree_if_clean(repo_root, worktree_root, 1, worktree_path)

    assert result.kind is WorktreePruneKind.PRUNED
    assert not worktree_path.exists()


def test_prune_worktree_if_clean_refuses_when_file_modified_before_removal(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    worktree_root = repo_root / ".ai" / "worktrees"
    worktree_path = worktree_root / "issue-1"
    repo_root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo_root)], check=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    tracked = repo_root / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "--quiet", "-m", "initial"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "add", "--quiet", str(worktree_path)],
        check=True,
    )
    task_path = worktree_path / ".ai" / "tasks" / "1.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("scheduler state\n", encoding="utf-8")

    original_run = subprocess.run

    def modify_on_status(*args: Any, **kwargs: Any) -> Any:
        res = original_run(*args, **kwargs)
        cmd = args[0] if args else kwargs.get("args", [])
        if len(cmd) >= 4 and cmd[1:4] == ["-C", str(worktree_path), "status"]:
            # External process modifies tracked file right after status inspection passes
            (worktree_path / "tracked.txt").write_text(
                "concurrent edit\n", encoding="utf-8"
            )
        return res

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subprocess, "run", modify_on_status)
        result = prune_worktree_if_clean(repo_root, worktree_root, 1, worktree_path)

    assert result.kind is WorktreePruneKind.FAILED
    assert "git worktree remove failed" in result.reason
    assert worktree_path.exists()
    assert (
        worktree_path / "tracked.txt"
    ).read_text(encoding="utf-8") == "concurrent edit\n"


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
