import json
import subprocess
from pathlib import Path

import pytest

from subsched.github.issues import GitHubCliError, GitHubIssueSource, diagnose_token

FIXTURES = Path(__file__).parent.parent / "fixtures" / "github"


def test_github_adapter_uses_argv_and_validates_json(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='[{"number":2,"title":"Two","body":"","labels":[{"name":"ai-ready"}],"url":"u"}]',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    issues = GitHubIssueSource().list_open("owner/project", label="ai-ready")

    assert issues[0].number == 2
    assert issues[0].labels == ("ai-ready",)
    assert calls == [
        [
            "gh",
            "issue",
            "list",
            "--repo",
            "owner/project",
            "--state",
            "open",
            "--limit",
            "1000",
            "--label",
            "ai-ready",
            "--json",
            "number,title,body,labels,url",
        ]
    ]


def test_github_adapter_redacts_cli_failure() -> None:
    def failed(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="authentication failed")

    source = GitHubIssueSource(run=failed)

    with pytest.raises(GitHubCliError, match="stderr hidden") as error:
        source.list_open("owner/project")
    assert "authentication failed" not in str(error.value)


def test_github_adapter_default_limit_and_custom_limit() -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    source = GitHubIssueSource(run=fake_run)
    source.list_open("owner/project")
    assert calls[0][calls[0].index("--limit") + 1] == "1000"

    source.list_open("owner/project", limit=250)
    assert calls[1][calls[1].index("--limit") + 1] == "250"


def test_github_adapter_parses_over_100_issues_without_truncation() -> None:
    """If `gh` is ever asked for and returns >100 issues, parsing itself does not truncate."""
    fixture = json.loads((FIXTURES / "issue-list-over-100.json").read_text())
    assert len(fixture) == 105

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(fixture), stderr="")

    issues = GitHubIssueSource(run=fake_run).list_open("owner/project")

    assert len(issues) == 105
    assert {issue.number for issue in issues} == set(range(1, 106))


def test_diagnose_token_never_requests_token_value() -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        payload = (
            '{"github.com":[{"active":true,"login":"octocat",'
            '"scopes":"gist, read:org, repo, workflow","state":"success"}]}'
        )
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    diagnose_token(run=fake_run)

    assert calls == [["gh", "auth", "status", "--json", "hosts"]]
    assert "--show-token" not in calls[0]


def test_diagnose_token_reports_broad_write_scopes() -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        payload = (
            '{"github.com":[{"active":true,"login":"octocat",'
            '"scopes":"gist, read:org, repo, workflow","state":"success"}]}'
        )
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    diagnosis = diagnose_token(run=fake_run)

    assert diagnosis.authenticated is True
    assert diagnosis.scopes == ("gist", "read:org", "repo", "workflow")
    assert diagnosis.can_discover is True
    assert diagnosis.can_write is True
    assert diagnosis.broad_scopes == ("repo", "workflow")


def test_diagnose_token_read_only_scope_reports_no_write() -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        payload = '{"github.com":[{"active":true,"login":"octocat","scopes":"","state":"success"}]}'
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    diagnosis = diagnose_token(run=fake_run)

    assert diagnosis.authenticated is True
    assert diagnosis.scopes == ()
    assert diagnosis.can_write is False
    assert diagnosis.broad_scopes == ()


def test_diagnose_token_finds_active_account_on_non_first_host() -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        payload = (
            '{"github.enterprise.example":[{"active":false,"scopes":"repo"}],'
            '"github.com":[{"active":true,"scopes":"read:org"}]}'
        )
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    diagnosis = diagnose_token(run=fake_run)

    assert diagnosis.authenticated is True
    assert diagnosis.scopes == ("read:org",)
    assert diagnosis.can_write is False


def test_diagnose_token_fails_closed_when_unauthenticated() -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not logged in")

    diagnosis = diagnose_token(run=fake_run)

    assert diagnosis.authenticated is False
    assert diagnosis.scopes == ()
    assert diagnosis.can_discover is False
    assert diagnosis.can_write is False


def test_diagnose_token_fails_closed_on_malformed_json() -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="not json", stderr="")

    diagnosis = diagnose_token(run=fake_run)

    assert diagnosis.authenticated is False
    assert diagnosis.scopes == ()
