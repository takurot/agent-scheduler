from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from subsched.github.pull_requests import (
    PullRequestResultKind,
    build_pr_body,
    create_or_get_pull_request,
    find_close_keyword_commits,
    lookup_existing_pr,
)
from subsched.models import Issue, Task


def _git_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True)
    subprocess.run(["git", "branch", "feature"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "feature"], cwd=repo_dir, check=True, capture_output=True)
    return repo_dir


def _commit(repo_dir: Path, filename: str, message: str) -> None:
    path = repo_dir / filename
    path.write_text(message, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo_dir, check=True, capture_output=True)


def test_build_pr_body_avoids_fixes_or_closes() -> None:
    body = build_pr_body(
        103, summary="Fixes #5 and Closes #6 bug", verification_results="Resolves #7 passed"
    )
    assert "Implements work for #103" in body
    assert "fixes #" not in body.lower()
    assert "closes #" not in body.lower()
    assert "resolves #" not in body.lower()
    assert "Issue is intentionally left open until review." in body


def test_lookup_existing_pr_found(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_json = json.dumps([
        {
            "number": 66,
            "url": "https://github.com/takurot/agent-scheduler/pull/66",
            "title": "Verification runner (#26)",
            "body": "Implements work for #26",
        }
    ])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 0, stdout=fake_json, stderr=""),
    )
    pr = lookup_existing_pr("issue/26-verification-runner")
    assert pr is not None
    assert pr.number == 66
    assert "/pull/66" in pr.url


def test_create_or_get_pull_request_creates_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(
            argv, 0, stdout="https://github.com/takurot/agent-scheduler/pull/68\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    task = Task.from_issue(Issue(number=103, title="Support timeout"))
    result = create_or_get_pull_request(task, "issue/103-timeout")
    assert result.kind is PullRequestResultKind.SUCCESS
    assert result.info is not None
    assert result.info.number == 68
    assert len(calls) == 2


def test_create_or_get_pull_request_returns_failure_with_output_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for #128: a failed `gh pr create` must surface *why* it failed
    (redacted stderr/stdout), not just collapse to an unexplained None."""

    def fake_fail(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gh: command not found")

    monkeypatch.setattr(subprocess, "run", fake_fail)
    task = Task.from_issue(Issue(number=103, title="Support timeout"))
    result = create_or_get_pull_request(task, "issue/103-timeout")
    assert result.kind is PullRequestResultKind.FAILURE
    assert result.info is None
    assert "gh: command not found" in result.output


def test_find_close_keyword_commits_detects_closes_in_commit_body(tmp_path: Path) -> None:
    """Regression test for #140: PR #135's real dogfood commit ended its message body
    with a bare "Closes #130" line (not just a title). The commit-message gate must
    inspect the full message (%B), not just the subject line."""
    repo_dir = _git_repo(tmp_path)
    _commit(
        repo_dir,
        "a.txt",
        "fix: some real change\n\nDetails about the fix.\n\nCloses #130\n",
    )
    violations = find_close_keyword_commits(repo_dir, "main")
    assert violations is not None
    assert len(violations) == 1
    assert "130" in violations[0].keyword_context


@pytest.mark.parametrize(
    "message",
    [
        "fix: resolve the timeout bug (issue #130)",
        "feat: implements work for #130",
        "docs: mention issue #130 in the changelog",
    ],
)
def test_find_close_keyword_commits_allows_safe_references(tmp_path: Path, message: str) -> None:
    repo_dir = _git_repo(tmp_path)
    _commit(repo_dir, "a.txt", message)
    violations = find_close_keyword_commits(repo_dir, "main")
    assert violations == ()


@pytest.mark.parametrize(
    "message",
    [
        "fix: bug\n\nFixes #130",
        "fix: bug\n\nfixes #130",
        "fix: bug\n\nCLOSES #130",
        "fix: bug\n\nResolved #130",
        "fix: bug\n\nClose #130",
    ],
)
def test_find_close_keyword_commits_detects_case_and_inflection_variants(
    tmp_path: Path, message: str
) -> None:
    repo_dir = _git_repo(tmp_path)
    _commit(repo_dir, "a.txt", message)
    violations = find_close_keyword_commits(repo_dir, "main")
    assert violations is not None
    assert len(violations) == 1


def test_find_close_keyword_commits_only_scans_new_commits(tmp_path: Path) -> None:
    """Only commits reachable from HEAD but not from base_branch are scanned -- pre-existing
    history on main (which the Scheduler doesn't control) must not trigger escalation."""
    repo_dir = _git_repo(tmp_path)
    subprocess.run(["git", "checkout", "main"], cwd=repo_dir, check=True, capture_output=True)
    _commit(repo_dir, "b.txt", "chore: unrelated main history\n\nCloses #999")
    subprocess.run(["git", "checkout", "feature"], cwd=repo_dir, check=True, capture_output=True)
    _commit(repo_dir, "a.txt", "fix: safe change referencing issue #130")

    violations = find_close_keyword_commits(repo_dir, "main")
    assert violations == ()


def test_find_close_keyword_commits_does_not_rewrite_history(tmp_path: Path) -> None:
    repo_dir = _git_repo(tmp_path)
    _commit(repo_dir, "a.txt", "fix: bug\n\nCloses #130")
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    ).stdout.strip()

    find_close_keyword_commits(repo_dir, "main")

    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert before == after


def test_find_close_keyword_commits_fails_closed_on_git_error(tmp_path: Path) -> None:
    result = find_close_keyword_commits(tmp_path / "not-a-repo", "main")
    assert result is None
