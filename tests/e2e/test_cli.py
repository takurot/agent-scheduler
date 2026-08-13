import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from subsched.cli import app
from subsched.github.issues import GitHubIssueSource
from subsched.models import Issue

runner = CliRunner()


@pytest.fixture(autouse=True)
def mock_github(monkeypatch: pytest.MonkeyPatch) -> None:
    def list_open(
        self: GitHubIssueSource, repo: str, *, label: str | None = None
    ) -> tuple[Issue, ...]:
        return tuple(Issue(number=number, title=f"Issue {number}") for number in (1, 7, 101, 103))

    monkeypatch.setattr(GitHubIssueSource, "list_open", list_open)


def invoke(repository: Path, *arguments: str) -> Result:
    return runner.invoke(app, ["--repository", str(repository), *arguments])


def test_explicit_issue_dry_run_persists_queue_and_status(tmp_path: Path) -> None:
    result = invoke(tmp_path, "run", "--repo", "owner/project", "--issues", "103,101", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "2 issue(s) discovered" in result.output
    status = invoke(tmp_path, "status")
    assert status.exit_code == 0
    assert "READY" in status.output
    assert "2" in status.output


def test_run_requires_exactly_one_selection_mode(tmp_path: Path) -> None:
    result = invoke(
        tmp_path,
        "run",
        "--repo",
        "owner/project",
        "--label",
        "ai-ready",
        "--issues",
        "all-open",
        "--dry-run",
    )

    assert result.exit_code != 0
    assert "exactly one" in result.output


def test_pause_resume_and_cancel_preserve_worktree_state(tmp_path: Path) -> None:
    assert (
        invoke(tmp_path, "run", "--repo", "owner/project", "--issues", "7", "--dry-run").exit_code
        == 0
    )

    assert invoke(tmp_path, "pause").exit_code == 0
    assert "PAUSED" in invoke(tmp_path, "status").output
    assert invoke(tmp_path, "resume").exit_code == 0
    assert invoke(tmp_path, "cancel", "7").exit_code == 0
    assert "CANCELLED" in invoke(tmp_path, "status").output


def test_native_execution_is_fail_closed(tmp_path: Path) -> None:
    result = invoke(tmp_path, "run", "--repo", "owner/project", "--issues", "1")

    assert result.exit_code != 0
    assert "native workers are not enabled" in result.output


def test_excluded_issue_is_not_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def list_open(
        self: GitHubIssueSource, repo: str, *, label: str | None = None
    ) -> tuple[Issue, ...]:
        return (Issue(number=8, title="blocked", labels=("blocked",)),)

    monkeypatch.setattr(GitHubIssueSource, "list_open", list_open)

    result = invoke(tmp_path, "run", "--repo", "owner/project", "--issues", "all-open", "--dry-run")

    assert result.exit_code == 0
    assert "0 issue(s) discovered" in result.output


def test_doctor_warns_on_broad_token_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda command: f"/usr/bin/{command}")

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        payload = (
            '{"github.com":[{"active":true,"scopes":"repo, workflow","state":"success"}]}'
        )
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = invoke(tmp_path, "doctor")

    assert "repo" in result.output
    assert "workflow" in result.output
    assert "broader than required" in result.output.lower()
    assert "gho_" not in result.output
    assert "token=" not in result.output.lower()


def test_doctor_reports_no_warning_for_minimal_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda command: f"/usr/bin/{command}")

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        payload = '{"github.com":[{"active":true,"scopes":"","state":"success"}]}'
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = invoke(tmp_path, "doctor")

    assert "broader than required" not in result.output.lower()


def test_doctor_reports_unauthenticated_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda command: f"/usr/bin/{command}")

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not logged in")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = invoke(tmp_path, "doctor")

    assert "not authenticated" in result.output


def test_cli_enforces_task_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def list_open(
        self: GitHubIssueSource, repo: str, *, label: str | None = None
    ) -> tuple[Issue, ...]:
        return tuple(Issue(number=number, title=str(number)) for number in range(1, 52))

    monkeypatch.setattr(GitHubIssueSource, "list_open", list_open)

    result = invoke(tmp_path, "run", "--repo", "owner/project", "--issues", "all-open", "--dry-run")

    assert result.exit_code != 0
    assert "Task limit exceeded (50)" in result.output
