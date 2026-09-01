from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from subsched.gitenv import GIT_LOCATION_OVERRIDE_VARS
from subsched.github.conflict import (
    RebaseResult,
    RebaseStatus,
    handle_rebase_outcome,
    rebase_onto_base,
)
from subsched.models import Task, TaskState


@pytest.fixture
def git_repo_with_branch(tmp_path: Path) -> tuple[Path, Path]:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)

    readme = repo_dir / "README.md"
    readme.write_text("# Main Branch\nInitial content\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True)

    remote_dir = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=repo_dir, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo_dir, check=True)

    # Create feature branch worktree
    feature_dir = tmp_path / "worktree-101"
    subprocess.run(
        ["git", "worktree", "add", "-b", "subsched/issue-101", str(feature_dir), "main"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    return repo_dir, feature_dir


def test_rebase_clean(git_repo_with_branch: tuple[Path, Path]) -> None:
    repo_dir, feature_dir = git_repo_with_branch

    # Commit on main
    new_file = repo_dir / "other.txt"
    new_file.write_text("other\n", encoding="utf-8")
    subprocess.run(["git", "add", "other.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "main commit"], cwd=repo_dir, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=repo_dir, check=True)

    # Commit on feature
    feat_file = feature_dir / "feature.txt"
    feat_file.write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=feature_dir, check=True)
    subprocess.run(["git", "commit", "-m", "feature commit"], cwd=feature_dir, check=True)

    result = rebase_onto_base(feature_dir, base_branch="main")
    assert result.status == RebaseStatus.SUCCESS
    assert result.conflicted_files == ()


def test_rebase_conflict_detects_and_aborts(git_repo_with_branch: tuple[Path, Path]) -> None:
    repo_dir, feature_dir = git_repo_with_branch

    # Commit change to README on main
    readme_main = repo_dir / "README.md"
    readme_main.write_text("# Main Branch\nConflicting change from main\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "conflict on main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=repo_dir, check=True)

    # Commit conflicting change to README on feature
    readme_feat = feature_dir / "README.md"
    readme_feat.write_text("# Main Branch\nConflicting change from feature\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=feature_dir, check=True)
    subprocess.run(["git", "commit", "-m", "conflict on feature"], cwd=feature_dir, check=True)

    result = rebase_onto_base(feature_dir, base_branch="main")
    assert result.status == RebaseStatus.CONFLICT
    assert "README.md" in result.conflicted_files

    # Ensure rebase was cleanly aborted (git status is clean, not in rebase state)
    status_proc = subprocess.run(
        ["git", "status"],
        cwd=feature_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "rebase in progress" not in status_proc.stdout.casefold()


def test_rebase_onto_base_strips_git_location_override_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaked GIT_DIR/GIT_WORK_TREE must never redirect rebase/diff/abort calls away from
    `worktree_dir` (issue #147)."""
    monkeypatch.setenv("GIT_DIR", "/leaked/.git")
    calls: list[dict[str, object]] = []
    real_run = subprocess.run

    def spying_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", spying_run)

    rebase_onto_base(tmp_path, base_branch="non-existent")

    assert calls, "expected at least one git invocation"
    for kwargs in calls:
        env = kwargs.get("env")
        assert isinstance(env, dict), "every git call must pass an explicit env"
        for name in GIT_LOCATION_OVERRIDE_VARS:
            assert name not in env


def test_rebase_invalid_branch(tmp_path: Path) -> None:
    result = rebase_onto_base(tmp_path, base_branch="non-existent")
    assert result.status in {RebaseStatus.FAILURE, RebaseStatus.CONFLICT}


def test_rebase_fetch_failure_does_not_attempt_rebase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="secret remote detail"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = rebase_onto_base(tmp_path, base_branch="develop")

    assert result.status is RebaseStatus.FAILURE
    assert "fetch failed" in result.output
    assert "secret remote detail" not in result.output
    assert calls == [["git", "fetch", "--no-tags", "origin", "--", "develop"]]


def test_rebase_fetches_and_uses_remote_tracking_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = rebase_onto_base(tmp_path, base_branch="develop")

    assert result.status is RebaseStatus.SUCCESS
    assert calls == [
        ["git", "fetch", "--no-tags", "origin", "--", "develop"],
        ["git", "rebase", "refs/remotes/origin/develop"],
    ]


def test_rebase_reports_already_up_to_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        stdout = "Current branch is up to date.\n" if calls == 2 else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = rebase_onto_base(tmp_path, base_branch="main")

    assert result.status is RebaseStatus.ALREADY_UP_TO_DATE


def test_rebase_uses_remote_update_when_local_base_is_stale(
    git_repo_with_branch: tuple[Path, Path], tmp_path: Path
) -> None:
    repo_dir, feature_dir = git_repo_with_branch
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    local_main_before = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    publisher = tmp_path / "publisher"
    subprocess.run(["git", "clone", remote, str(publisher)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Publisher"], cwd=publisher, check=True)
    subprocess.run(
        ["git", "config", "user.email", "publisher@example.com"],
        cwd=publisher,
        check=True,
    )
    (publisher / "remote-only.txt").write_text("new remote base\n", encoding="utf-8")
    subprocess.run(["git", "add", "remote-only.txt"], cwd=publisher, check=True)
    subprocess.run(["git", "commit", "-m", "remote base update"], cwd=publisher, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=publisher, check=True)

    (feature_dir / "feature.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=feature_dir, check=True)
    subprocess.run(["git", "commit", "-m", "feature"], cwd=feature_dir, check=True)

    result = rebase_onto_base(feature_dir, base_branch="main")

    local_main_after = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert result.status is RebaseStatus.SUCCESS
    assert local_main_after == local_main_before
    assert (feature_dir / "remote-only.txt").read_text(encoding="utf-8") == "new remote base\n"


def test_rebase_subprocess_exception(tmp_path: Path) -> None:
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1)):
        result = rebase_onto_base(tmp_path, base_branch="main")
        assert result.status == RebaseStatus.FAILURE
        assert "timed out" in result.output.casefold() or "execution failed" in result.output


def test_handle_rebase_outcome() -> None:
    task = Task(
        task_id="github-101",
        issue_number=101,
        title="Task 101",
        labels=(),
        status=TaskState.NEEDS_REBASE,
    )

    success_result = RebaseResult(status=RebaseStatus.SUCCESS)
    updated, msg = handle_rebase_outcome(task, success_result)
    assert updated.status == TaskState.READY
    assert "cleanly" in msg

    # PR_READY on success
    pr_task = Task(
        task_id="github-102",
        issue_number=102,
        title="Task 102",
        labels=(),
        status=TaskState.PR_READY,
    )
    updated_pr, _ = handle_rebase_outcome(pr_task, success_result)
    assert updated_pr.status == TaskState.PR_READY

    conflict_result = RebaseResult(
        status=RebaseStatus.CONFLICT, conflicted_files=("README.md",)
    )
    updated, msg = handle_rebase_outcome(task, conflict_result)
    assert updated.status == TaskState.NEEDS_HUMAN
    assert "conflict" in msg.casefold()
    # Regression test for #128: the escalation reason must be persisted on the task,
    # not just returned as a local message that the caller can drop.
    assert updated.needs_human_reason == msg

    # IN_PROGRESS on conflict
    ip_task = Task(
        task_id="github-103",
        issue_number=103,
        title="Task 103",
        labels=(),
        status=TaskState.IN_PROGRESS,
    )
    updated_ip, ip_msg = handle_rebase_outcome(ip_task, conflict_result)
    assert updated_ip.status == TaskState.NEEDS_HUMAN
    assert updated_ip.needs_human_reason == ip_msg

    # IN_PROGRESS on failure
    fail_result = RebaseResult(status=RebaseStatus.FAILURE, output="fatal error")
    updated_ip_fail, fail_msg = handle_rebase_outcome(ip_task, fail_result)
    assert updated_ip_fail.status == TaskState.NEEDS_HUMAN
    assert updated_ip_fail.needs_human_reason == fail_msg
    assert "fatal error" in fail_msg

    # FAILED task
    failed_task = Task(
        task_id="github-104",
        issue_number=104,
        title="Task 104",
        labels=(),
        status=TaskState.FAILED,
    )
    updated_f, _ = handle_rebase_outcome(failed_task, conflict_result)
    assert updated_f.status == TaskState.FAILED
    updated_f2, _ = handle_rebase_outcome(failed_task, fail_result)
    assert updated_f2.status == TaskState.FAILED
