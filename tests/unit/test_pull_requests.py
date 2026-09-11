from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from subsched.gitenv import GIT_LOCATION_OVERRIDE_VARS
from subsched.github.pull_requests import (
    MAX_PR_SECTION_CHARS,
    ExistingPrCheckKind,
    MergedPrCheckKind,
    PullRequestResultKind,
    build_pr_body,
    check_merged_pr_for_issue,
    create_or_get_pull_request,
    extract_pr_summary,
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


def test_build_pr_body_with_close_issue_true() -> None:
    body = build_pr_body(
        103,
        summary="Fixes #5 and Closes #6 bug",
        verification_results="Resolves #7 passed",
        close_issue=True,
    )
    assert "Implements work for #103" in body
    # User-supplied sections must still sanitize auto-close keywords
    assert "fixes #" not in body.lower()
    assert "resolves #" not in body.lower()
    # But the Scheduler-appended footer explicitly includes Closes #103.
    assert "Closes #103." in body
    assert "Issue is intentionally left open until review." not in body


def test_build_pr_body_redacts_all_user_supplied_sections() -> None:
    secret = "github_pat_abcdefghijklmnopqrstuvwxyz"

    body = build_pr_body(
        103,
        summary=f"updated with {secret}",
        verification_results=f"failed with {secret}",
    )

    assert body.startswith("Implements work for #103.\n")
    assert body.count("[REDACTED]") == 2
    assert secret not in body


def test_build_pr_body_truncates_verification_section() -> None:
    marker = "end-marker"

    body = build_pr_body(
        103,
        verification_results="x" * (MAX_PR_SECTION_CHARS + 1) + marker,
    )

    verification_section = body.split("## Verification\n\n", 1)[1]
    assert "... [truncated]" in verification_section
    assert marker not in verification_section


def test_lookup_existing_pr_found(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_json = json.dumps([
        {
            "number": 66,
            "url": "https://github.com/takurot/agent-scheduler/pull/66",
            "title": "Verification runner (#26)",
            "body": "Implements work for #26.\n\n## Summary\n...",
            "headRefName": "issue/26-verification-runner",
            "baseRefName": "main",
        }
    ])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 0, stdout=fake_json, stderr=""),
    )
    result = lookup_existing_pr("issue/26-verification-runner", issue_number=26, base="main")
    assert result.kind is ExistingPrCheckKind.CONFIRMED
    assert result.info is not None
    assert result.info.number == 66
    assert "/pull/66" in result.info.url


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


def test_create_or_get_pull_request_with_close_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(
            argv, 0, stdout="https://github.com/takurot/agent-scheduler/pull/69\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    task = Task.from_issue(Issue(number=103, title="Support timeout"))
    result = create_or_get_pull_request(task, "issue/103-timeout", close_issue=True)
    assert result.kind is PullRequestResultKind.SUCCESS
    create_call = next(c for c in calls if "create" in c)
    body_idx = create_call.index("--body") + 1
    body_arg = create_call[body_idx]
    assert "Closes #103." in body_arg
    assert "Issue is intentionally left open until review." not in body_arg


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


def test_find_close_keyword_commits_strips_git_location_override_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaked GIT_DIR/GIT_WORK_TREE must never redirect this read-only `git log` away
    from `worktree_dir` (issue #147 follow-up: this call site was missed in the initial
    git_safe_env() rollout)."""
    monkeypatch.setenv("GIT_DIR", "/leaked/.git")
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    find_close_keyword_commits(tmp_path, "main")

    env = captured.get("env")
    assert isinstance(env, dict)
    for name in GIT_LOCATION_OVERRIDE_VARS:
        assert name not in env


def _write_handoff(repo_dir: Path, issue_number: int, completed: str, decisions: str) -> None:
    handoffs_dir = repo_dir / ".ai" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    (handoffs_dir / f"{issue_number}.md").write_text(
        f"""# Issue

#{issue_number} Some title

## Goal

Some goal

## Current Plan

- plan

## Completed

{completed}

## Current Work

None (task completed)

## Decisions

{decisions}

## Known Broken State

None

## Next Action

None

## Timestamp

2026-09-11T00:00:00Z
""",
        encoding="utf-8",
    )


def test_extract_pr_summary_uses_handoff_completed_and_decisions(tmp_path: Path) -> None:
    repo_dir = _git_repo(tmp_path)
    _write_handoff(
        repo_dir,
        103,
        completed="- Implemented the widget\n- Added tests",
        decisions="- Chose approach X over Y because it was simpler",
    )
    task = Task.from_issue(Issue(number=103, title="Support timeout"))

    summary = extract_pr_summary(repo_dir, task, "main")

    assert "### Completed Changes" in summary
    assert "Implemented the widget" in summary
    assert "### Key Decisions" in summary
    assert "Chose approach X over Y" in summary


def test_extract_pr_summary_falls_back_to_git_log_when_handoff_missing(tmp_path: Path) -> None:
    repo_dir = _git_repo(tmp_path)
    _commit(repo_dir, "a.txt", "fix: correct the widget rendering\n\nDetails about the fix.")
    task = Task.from_issue(Issue(number=103, title="Support timeout"))

    summary = extract_pr_summary(repo_dir, task, "main")

    assert "### Commits" in summary
    assert "fix: correct the widget rendering" in summary
    assert "Details about the fix." in summary


def test_extract_pr_summary_falls_back_to_git_log_when_handoff_placeholder(tmp_path: Path) -> None:
    repo_dir = _git_repo(tmp_path)
    _write_handoff(repo_dir, 103, completed="- Task bootstrapped", decisions="- None yet")
    _commit(repo_dir, "a.txt", "fix: correct the widget rendering")
    task = Task.from_issue(Issue(number=103, title="Support timeout"))

    summary = extract_pr_summary(repo_dir, task, "main")

    assert "### Commits" in summary
    assert "fix: correct the widget rendering" in summary


def test_extract_pr_summary_falls_back_to_task_title_when_nothing_available(
    tmp_path: Path,
) -> None:
    repo_dir = _git_repo(tmp_path)
    task = Task.from_issue(Issue(number=103, title="Support timeout"))

    summary = extract_pr_summary(repo_dir, task, "main")

    assert summary == "Support timeout"


def test_extract_pr_summary_strips_auto_close_keywords_via_sanitize(tmp_path: Path) -> None:
    """Regression test for #249: a worker could write 'Fixes #N' into its own handoff;
    the summary must still be sanitized by build_pr_body before reaching the PR body."""
    repo_dir = _git_repo(tmp_path)
    _write_handoff(
        repo_dir,
        103,
        completed="- Fixes #999 by correcting the widget",
        decisions="- None yet",
    )
    task = Task.from_issue(Issue(number=103, title="Support timeout"))

    summary = extract_pr_summary(repo_dir, task, "main")
    body = build_pr_body(issue_number=103, summary=summary)

    assert "fixes #" not in body.lower()
    assert "issue #999" in body.lower()


def test_extract_pr_summary_redacts_secrets_via_sanitize(tmp_path: Path) -> None:
    secret = "github_pat_abcdefghijklmnopqrstuvwxyz"
    repo_dir = _git_repo(tmp_path)
    _write_handoff(
        repo_dir,
        103,
        completed=f"- updated with {secret}",
        decisions="- None yet",
    )
    task = Task.from_issue(Issue(number=103, title="Support timeout"))

    summary = extract_pr_summary(repo_dir, task, "main")
    body = build_pr_body(issue_number=103, summary=summary)

    assert secret not in body
    assert "[REDACTED]" in body


def test_create_or_get_pull_request_uses_extract_pr_summary_when_worktree_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_dir = _git_repo(tmp_path)
    _write_handoff(
        repo_dir,
        103,
        completed="- Implemented the widget",
        decisions="- None yet",
    )

    real_run = subprocess.run

    def fake_gh_only(argv, **kwargs):
        if argv[0] != "gh":
            return real_run(argv, **kwargs)
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(
            argv, 0, stdout="https://github.com/takurot/agent-scheduler/pull/70\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_gh_only)

    task = Task.from_issue(Issue(number=103, title="Support timeout")).with_worktree(
        str(repo_dir)
    )
    result = create_or_get_pull_request(task, "issue/103-timeout", base="main")

    assert result.kind is PullRequestResultKind.SUCCESS
