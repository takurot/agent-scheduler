from __future__ import annotations

import json
import subprocess

import pytest

from subsched.github.pull_requests import (
    MergedPrCheckKind,
    PullRequestResultKind,
    build_pr_body,
    check_merged_pr_for_issue,
    create_or_get_pull_request,
    lookup_existing_pr,
)
from subsched.models import Issue, Task


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


def test_check_merged_pr_for_issue_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test for #146: a merged PR created by the Scheduler's own
    create_or_get_pull_request (exact body prefix + subsched/issue-N branch) must be
    recognized as CONFIRMED so the issue isn't rediscovered as READY."""
    payload = json.dumps(
        [
            {
                "number": 131,
                "headRefName": "subsched/issue-129",
                "body": "Implements work for #129.\n\n## Summary\n\n...",
            }
        ]
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 0, stdout=payload, stderr=""),
    )
    result = check_merged_pr_for_issue("owner/repo", 129)
    assert result.kind is MergedPrCheckKind.CONFIRMED
    assert result.pr_number == 131


def test_check_merged_pr_for_issue_none_when_no_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 0, stdout="[]", stderr=""),
    )
    result = check_merged_pr_for_issue("owner/repo", 129)
    assert result.kind is MergedPrCheckKind.NONE


def test_check_merged_pr_for_issue_ambiguous_on_wrong_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for #146: this is the *real* observed case (PR #131 for issue
    #129 was actually created manually via `gh pr create`, with branch
    `issue/129-verification-uv-defaults`, not the Scheduler's `subsched/issue-129`
    convention). Untrusted GitHub content must not be trusted into CONFIRMED just
    because the body text loosely matches -- this must fail closed to AMBIGUOUS."""
    payload = json.dumps(
        [
            {
                "number": 131,
                "headRefName": "issue/129-verification-uv-defaults",
                "body": "## 対応Issue\nImplements work for #129。\n\n...",
            }
        ]
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 0, stdout=payload, stderr=""),
    )
    result = check_merged_pr_for_issue("owner/repo", 129)
    assert result.kind is MergedPrCheckKind.AMBIGUOUS
    assert "129" in result.reason


def test_check_merged_pr_for_issue_ambiguous_on_multiple_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        [
            {
                "number": 131,
                "headRefName": "subsched/issue-129",
                "body": "Implements work for #129.\n",
            },
            {
                "number": 200,
                "headRefName": "some-other-branch",
                "body": "unrelated PR that happens to mention #129",
            },
        ]
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 0, stdout=payload, stderr=""),
    )
    result = check_merged_pr_for_issue("owner/repo", 129)
    assert result.kind is MergedPrCheckKind.AMBIGUOUS


def test_check_merged_pr_for_issue_fails_closed_on_gh_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="error"),
    )
    result = check_merged_pr_for_issue("owner/repo", 129)
    assert result.kind is MergedPrCheckKind.AMBIGUOUS


def test_check_merged_pr_for_issue_fails_closed_on_subprocess_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: object, **k: object) -> object:
        raise OSError("gh not found")

    monkeypatch.setattr(subprocess, "run", boom)
    result = check_merged_pr_for_issue("owner/repo", 129)
    assert result.kind is MergedPrCheckKind.AMBIGUOUS
