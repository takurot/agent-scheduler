"""E2E tests for `subsched reconcile` (#277)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from subsched.cli import app
from subsched.models import Task, TaskState
from subsched.storage import JsonStateStore

runner = CliRunner()


def invoke(repository: Path, *arguments: str) -> Result:
    return runner.invoke(
        app, ["--repository", str(repository), *arguments], env={"NO_COLOR": "1"}
    )


def seed_task(
    repository: Path,
    issue_number: int,
    pr: int | None,
    status: TaskState = TaskState.READY_FOR_REVIEW,
) -> None:
    store = JsonStateStore(repository)
    store.init_directories()
    task = Task(
        task_id=f"github-{issue_number}",
        issue_number=issue_number,
        title=f"Task {issue_number}",
        labels=(),
        status=status,
        pr=pr,
    )
    store.save_tasks((task,))


_real_run = subprocess.run


def fake_gh_pr_list(states: dict[int, str]) -> object:
    """Fake `gh pr view <n>`: answers per tracked PR number (#377)."""

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[:3] != ["gh", "pr", "view"]:
            return _real_run(argv, **kwargs)  # type: ignore[arg-type]
        number = int(argv[3])
        if number not in states:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not found")
        return subprocess.CompletedProcess(
            argv, 0, stdout=f'{{"number": {number}, "state": "{states[number]}"}}', stderr=""
        )

    return fake_run


def test_reconcile_merged_pr_advances_task_to_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_task(tmp_path, 1, pr=10)
    monkeypatch.setattr(subprocess, "run", fake_gh_pr_list({10: "MERGED"}))

    result = invoke(tmp_path, "reconcile", "--repo", "owner/project")

    assert result.exit_code == 0, result.output
    assert "reconciled_complete=1" in result.output
    status = invoke(tmp_path, "status")
    assert "COMPLETE" in status.output


def test_reconcile_closed_pr_escalates_to_needs_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_task(tmp_path, 2, pr=20)
    monkeypatch.setattr(subprocess, "run", fake_gh_pr_list({20: "CLOSED"}))

    result = invoke(tmp_path, "reconcile", "--repo", "owner/project")

    assert result.exit_code == 0, result.output
    assert "reconciled_needs_human=1" in result.output
    status = invoke(tmp_path, "status")
    assert "NEEDS_HUMAN" in status.output


def test_reconcile_open_pr_leaves_task_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_task(tmp_path, 3, pr=30)
    monkeypatch.setattr(subprocess, "run", fake_gh_pr_list({30: "OPEN"}))

    result = invoke(tmp_path, "reconcile", "--repo", "owner/project")

    assert result.exit_code == 0, result.output
    assert "unchanged=1" in result.output
    status = invoke(tmp_path, "status")
    assert "READY_FOR_REVIEW" in status.output


def test_reconcile_dry_run_does_not_mutate_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_task(tmp_path, 4, pr=40)
    monkeypatch.setattr(subprocess, "run", fake_gh_pr_list({40: "MERGED"}))

    result = invoke(tmp_path, "reconcile", "--repo", "owner/project", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    status = invoke(tmp_path, "status")
    assert "READY_FOR_REVIEW" in status.output


def test_reconcile_resolves_old_tracked_prs_with_bounded_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#377: tracked PRs far older than any recent-PR window are still reconciled, and
    dry-run reports the same result with exactly one gh call per tracked PR."""
    seed_task(tmp_path, 1, pr=1)
    store = JsonStateStore(tmp_path)
    store.save_tasks(
        (
            *store.load_tasks(),
            Task(
                task_id="github-2",
                issue_number=2,
                title="Task 2",
                labels=(),
                status=TaskState.READY_FOR_REVIEW,
                pr=900,
            ),
        )
    )
    gh_run = fake_gh_pr_list({1: "MERGED", 900: "OPEN"})
    view_calls: list[list[str]] = []

    def counting_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["gh", "pr", "view"]:
            view_calls.append(argv)
        return gh_run(argv, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(subprocess, "run", counting_run)

    dry = invoke(tmp_path, "reconcile", "--repo", "owner/project", "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "reconciled_complete=1" in dry.output
    assert "unchanged=1" in dry.output
    assert len(view_calls) == 2

    real = invoke(tmp_path, "reconcile", "--repo", "owner/project")
    assert real.exit_code == 0, real.output
    assert "reconciled_complete=1" in real.output
    assert len(view_calls) == 4
    states = {task.issue_number: task.status for task in store.load_tasks()}
    assert states == {1: TaskState.COMPLETE, 2: TaskState.READY_FOR_REVIEW}


def test_reconcile_fails_closed_on_gh_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_task(tmp_path, 5, pr=50)

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not authenticated")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = invoke(tmp_path, "reconcile", "--repo", "owner/project")

    assert result.exit_code != 0
    assert "Failed to fetch PR state" in result.output
    status = invoke(tmp_path, "status")
    assert "READY_FOR_REVIEW" in status.output


def test_reconcile_requires_repo_when_not_configured(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()

    result = invoke(tmp_path, "reconcile")

    assert result.exit_code != 0
    clean_output = re.sub(r"\x1b\[[0-9;]*[mK]", "", result.output)
    assert "--repo" in clean_output


def test_reconcile_with_no_candidates_is_a_no_op(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()

    result = invoke(tmp_path, "reconcile", "--repo", "owner/project")

    assert result.exit_code == 0, result.output
    assert "No READY_FOR_REVIEW tasks" in result.output


def test_reconcile_prune_worktrees_removes_clean_worktree_after_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    worktree_path = store.worktrees_dir / "issue-6"
    worktree_path.mkdir(parents=True)
    task = Task(
        task_id="github-6",
        issue_number=6,
        title="Task 6",
        labels=(),
        status=TaskState.READY_FOR_REVIEW,
        pr=60,
        worktree=str(worktree_path),
    )
    store.save_tasks((task,))

    gh_run = fake_gh_pr_list({60: "MERGED"})
    removed: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["gh", "pr", "view"]:
            return gh_run(argv, **kwargs)  # type: ignore[no-any-return]
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if "remove" in argv:
            removed.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return _real_run(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = invoke(tmp_path, "reconcile", "--repo", "owner/project", "--prune-worktrees")

    assert result.exit_code == 0, result.output
    assert "PRUNED" in result.output
    assert removed
