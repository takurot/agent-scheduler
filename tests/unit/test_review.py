from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from subsched.review import (
    git_head_commit,
    read_review_report,
    review_report_path,
    worktree_touched_unexpected_paths,
)


def _init_repo(repo_dir: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True)


def _write_report(worktree_dir: Path, issue_number: int, round_number: int) -> None:
    report = review_report_path(worktree_dir, issue_number, round_number)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Review\n\n## Verdict\nAPPROVE\n", encoding="utf-8")


def test_untracked_reviews_directory_is_not_flagged(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_report(tmp_path, issue_number=1, round_number=1)

    assert worktree_touched_unexpected_paths(tmp_path, issue_number=1, round_number=1) is False


def test_prior_round_reports_are_not_flagged(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_report(tmp_path, issue_number=1, round_number=1)
    _write_report(tmp_path, issue_number=1, round_number=2)

    assert worktree_touched_unexpected_paths(tmp_path, issue_number=1, round_number=2) is False


def test_unexpected_file_is_flagged(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_report(tmp_path, issue_number=1, round_number=1)
    (tmp_path / "unexpected.txt").write_text("oops\n", encoding="utf-8")

    assert worktree_touched_unexpected_paths(tmp_path, issue_number=1, round_number=1) is True


def test_committed_change_bypassing_status_is_flagged_via_head_check(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    pre_dispatch_head = git_head_commit(tmp_path)
    _write_report(tmp_path, issue_number=1, round_number=1)
    (tmp_path / "sneaky.txt").write_text("sneaky\n", encoding="utf-8")
    subprocess.run(["git", "add", "sneaky.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "sneaky commit"], cwd=tmp_path, check=True)

    assert (
        worktree_touched_unexpected_paths(
            tmp_path, issue_number=1, round_number=1, pre_dispatch_head=pre_dispatch_head
        )
        is True
    )


def test_head_unchanged_is_not_flagged(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    pre_dispatch_head = git_head_commit(tmp_path)
    _write_report(tmp_path, issue_number=1, round_number=1)

    assert (
        worktree_touched_unexpected_paths(
            tmp_path, issue_number=1, round_number=1, pre_dispatch_head=pre_dispatch_head
        )
        is False
    )


def test_read_review_report_returns_none_on_undecodable_bytes(tmp_path: Path) -> None:
    report = review_report_path(tmp_path, issue_number=1, round_number=1)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_bytes(b"\xff\xfe# Review invalid utf-8 \x80\x81")

    assert read_review_report(tmp_path, issue_number=1, round_number=1) is None


def test_untracked_scaffold_files_in_unignored_repo_are_not_flagged(tmp_path: Path) -> None:
    # #325: Repos without .ai/ in .gitignore have untracked scaffold files created by Scheduler
    _init_repo(tmp_path)
    (tmp_path / ".ai" / "tasks").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "handoffs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "checkpoints").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "tasks" / "1.md").write_text("# Task\n", encoding="utf-8")
    (tmp_path / ".ai" / "handoffs" / "1.md").write_text("# Handoff\n", encoding="utf-8")
    (tmp_path / ".ai" / "checkpoints" / "1.json").write_text("{}", encoding="utf-8")
    _write_report(tmp_path, issue_number=1, round_number=1)

    assert worktree_touched_unexpected_paths(tmp_path, issue_number=1, round_number=1) is False


def test_modified_scaffold_files_are_flagged(tmp_path: Path) -> None:
    # If a scaffold file was tracked and modified, it should be flagged as unexpected
    _init_repo(tmp_path)
    (tmp_path / ".ai" / "tasks").mkdir(parents=True, exist_ok=True)
    task_file = tmp_path / ".ai" / "tasks" / "1.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    subprocess.run(["git", "add", str(task_file)], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "commit task file"], cwd=tmp_path, check=True)

    _write_report(tmp_path, issue_number=1, round_number=1)
    task_file.write_text("# Modified Task\n", encoding="utf-8")

    assert worktree_touched_unexpected_paths(tmp_path, issue_number=1, round_number=1) is True


def test_scaffold_files_for_other_issues_are_flagged(tmp_path: Path) -> None:
    # Scaffold file for issue 2 should be flagged when running review for issue 1
    _init_repo(tmp_path)
    (tmp_path / ".ai" / "tasks").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "tasks" / "2.md").write_text("# Task 2\n", encoding="utf-8")
    _write_report(tmp_path, issue_number=1, round_number=1)

    assert worktree_touched_unexpected_paths(tmp_path, issue_number=1, round_number=1) is True


def test_future_round_report_is_flagged(tmp_path: Path) -> None:
    # #325: future round reports must not be tolerated
    _init_repo(tmp_path)
    _write_report(tmp_path, issue_number=1, round_number=1)
    _write_report(tmp_path, issue_number=1, round_number=2)

    # When evaluating round 1, round 2 report is in the future
    assert worktree_touched_unexpected_paths(tmp_path, issue_number=1, round_number=1) is True


def test_deleted_prior_round_report_is_flagged(tmp_path: Path) -> None:
    # #325: deletion of a tracked prior round report must be flagged
    _init_repo(tmp_path)
    _write_report(tmp_path, issue_number=1, round_number=1)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "commit r1"], cwd=tmp_path, check=True)

    # Reviewer creates r2 and deletes tracked r1
    _write_report(tmp_path, issue_number=1, round_number=2)
    (tmp_path / ".ai" / "reviews" / "1-r1.md").unlink()

    assert worktree_touched_unexpected_paths(tmp_path, issue_number=1, round_number=2) is True


def test_missing_pre_dispatch_head_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # #325: pre_dispatch_head=None logs a warning about skipped HEAD movement check
    import logging

    _init_repo(tmp_path)
    _write_report(tmp_path, issue_number=1, round_number=1)

    with caplog.at_level(logging.WARNING):
        result = worktree_touched_unexpected_paths(
            tmp_path, issue_number=1, round_number=1, pre_dispatch_head=None
        )
    assert result is False
    assert any("pre_dispatch_head was not provided" in record.message for record in caplog.records)


def test_modified_current_round_report_is_flagged(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_report(tmp_path, issue_number=1, round_number=1)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "commit report"], cwd=tmp_path, check=True)

    # Modify the tracked current round report -> status is ' M'
    report = review_report_path(tmp_path, issue_number=1, round_number=1)
    report.write_text("# Review modified\n", encoding="utf-8")

    assert worktree_touched_unexpected_paths(tmp_path, issue_number=1, round_number=1) is True


def test_malformed_porcelain_line_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    _write_report(tmp_path, issue_number=1, round_number=1)

    import subprocess as sp

    class FakeCompletedProcess:
        returncode = 0
        stdout = "?\n"

    monkeypatch.setattr(sp, "run", lambda *args, **kwargs: FakeCompletedProcess())
    assert worktree_touched_unexpected_paths(tmp_path, issue_number=1, round_number=1) is True


def test_build_pr_comment_body() -> None:
    from subsched.models import Task, TaskState
    from subsched.review import ReviewReport, ReviewVerdict, build_pr_comment_body

    task = Task(
        task_id="github-1",
        issue_number=1,
        title="test",
        labels=(),
        status=TaskState.IN_PROGRESS,
    )
    report = ReviewReport(
        verdict=ReviewVerdict.APPROVE,
        summary="All good",
        findings="No defects",
    )
    body = build_pr_comment_body(task, 1, report)
    assert "### Automated review (round 1) for issue #1" in body
    assert "**Verdict:** APPROVE" in body
    assert "All good" in body
    assert "No defects" in body
