from __future__ import annotations

import subprocess
from pathlib import Path

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
