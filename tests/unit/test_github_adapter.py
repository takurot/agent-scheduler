import subprocess

import pytest

from subsched.github.issues import GitHubCliError, GitHubIssueSource


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
            "100",
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
