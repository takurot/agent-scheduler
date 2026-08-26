import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from subsched.agents.base import ProcessExecutionRequest, ProcessExecutionResult
from subsched.cli import app
from subsched.github.issues import GitHubIssueSource
from subsched.github.pull_requests import PullRequestInfo, PullRequestResult, PullRequestResultKind
from subsched.models import Issue, Task, TaskState
from subsched.storage import JsonStateStore

runner = CliRunner()


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def _init_git_repo(path: Path) -> None:
    """Create a minimal real local git repository with one commit on `main`."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    _git(path, "commit", "--allow-empty", "-q", "-m", "init")


def _claude_success_stdout() -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 1,
            "result": "done",
        }
    )


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


def test_run_without_repository_option_uses_git_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `--repository` is omitted, state resolves to the git root, not a subdirectory cwd."""
    _init_git_repo(tmp_path)
    subdir = tmp_path / "src"
    subdir.mkdir()
    monkeypatch.chdir(subdir)

    result = runner.invoke(
        app, ["run", "--repo", "owner/project", "--issues", "101", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".ai" / "scheduler.json").exists()
    assert not (subdir / ".ai").exists()


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


def test_run_invalid_issue_formats(tmp_path: Path) -> None:
    assert invoke(tmp_path, "run", "--repo", "o/r", "--issues", "abc", "--dry-run").exit_code != 0
    assert invoke(tmp_path, "run", "--repo", "o/r", "--issues", "-1", "--dry-run").exit_code != 0
    assert invoke(tmp_path, "run", "--repo", "o/r", "--issues", "1,1", "--dry-run").exit_code != 0


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


def test_cancel_non_existent_issue(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    result = invoke(tmp_path, "cancel", "999")
    assert result.exit_code != 0
    assert "not in scheduler state" in result.output or "not found" in result.output


def test_cancel_invalid_state_transition(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    t = Task(
        task_id="github-101",
        issue_number=101,
        title="Task 101",
        labels=(),
        status=TaskState.COMPLETE,
    )
    store.save_tasks([t])
    result = invoke(tmp_path, "cancel", "101")
    assert result.exit_code != 0
    assert "cannot be cancelled" in result.output


def test_native_execution_is_fail_closed(tmp_path: Path) -> None:
    result = invoke(tmp_path, "run", "--repo", "owner/project", "--issues", "1")

    assert result.exit_code != 0
    assert "native workers require explicit opt-in" in result.output.lower()


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


def test_doctor_reports_missing_executables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda command: None)
    result = invoke(tmp_path, "doctor")
    assert result.exit_code != 0
    assert "MISSING" in result.output


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


def test_doctor_reports_unauthenticated_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_cli_runs_with_natural_language_query(tmp_path: Path) -> None:
    result = invoke(
        tmp_path, "run", "GitHubのopen issueをすべて実行", "--repo", "owner/project", "--dry-run"
    )
    assert result.exit_code == 0, result.output
    assert "4 issue(s) discovered" in result.output


def test_cli_runs_with_config_file(tmp_path: Path) -> None:
    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        "github:\n  repo: owner/project\n  mode: all-open\nexecution:\n  concurrency: 1\n",
        encoding="utf-8",
    )
    result = invoke(tmp_path, "run", "--config", str(config_file), "--dry-run")
    assert result.exit_code == 0, result.output
    assert "4 issue(s) discovered" in result.output


def test_cli_commands_catch_state_corruption_error(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()

    def corrupt() -> None:
        store.path.write_text("invalid json content", encoding="utf-8")

    # status
    corrupt()
    res_status = invoke(tmp_path, "status")
    assert res_status.exit_code == 1
    assert "State error:" in res_status.output

    # run
    corrupt()
    res_run = invoke(tmp_path, "run", "--repo", "owner/project", "--issues", "1", "--dry-run")
    assert res_run.exit_code == 1
    assert "State error:" in res_run.output

    # pause
    corrupt()
    res_pause = invoke(tmp_path, "pause")
    assert res_pause.exit_code == 1
    assert "State error:" in res_pause.output

    # resume
    corrupt()
    res_resume = invoke(tmp_path, "resume")
    assert res_resume.exit_code == 1
    assert "State error:" in res_resume.output

    # cancel
    corrupt()
    res_cancel = invoke(tmp_path, "cancel", "1")
    assert res_cancel.exit_code == 1
    assert "State error:" in res_cancel.output

    # metrics
    corrupt()
    res_metrics = invoke(tmp_path, "metrics")
    assert res_metrics.exit_code == 1
    assert "State error:" in res_metrics.output


def test_metrics_handles_report_write_failure(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()

    # Pass directory path as report file to cause OSError on write_text
    report_dir = tmp_path / "a_directory"
    report_dir.mkdir()

    result = invoke(tmp_path, "metrics", "--report", str(report_dir))
    assert result.exit_code == 0
    assert f"Failed to write report to {report_dir}" in result.output
    assert "Productivity Metrics" in result.output


def test_run_output_message_reflects_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    # Dry-run
    res_dry = invoke(tmp_path, "run", "--repo", "owner/project", "--issues", "1", "--dry-run")
    assert res_dry.exit_code == 0
    assert "(dry-run)" in res_dry.output

    # Native allow: real (remote-less) git repo, so the wiring runs for real instead of
    # failing worktree setup; with no remote configured the task ends up NEEDS_HUMAN after
    # a failed push, which is fine here -- this test only cares about the persisted-count
    # message, not the final task state. The process layer is mocked (per #120: fixing
    # NativeWorker's env handling means "claude"/"codex" now genuinely resolve via PATH, so
    # a real binary on the machine running the tests would otherwise actually get invoked).
    monkeypatch.setattr(
        "subsched.agents.claude.run_process_group",
        lambda request: ProcessExecutionResult(exit_code=0, stdout=_claude_success_stdout()),
    )
    tmp_path2 = tmp_path / "sub"
    tmp_path2.mkdir()
    _init_git_repo(tmp_path2)
    res_native = invoke(
        tmp_path2, "run", "--repo", "owner/project", "--issues", "1", "--allow-native"
    )
    assert res_native.exit_code == 0
    assert "discovered and persisted" in res_native.output
    assert "(dry-run)" not in res_native.output


def test_native_run_drives_scheduler_to_complete_and_opens_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end (per WORKFLOW.md's E2E policy: real git, no real provider or GitHub calls)
    check that `--allow-native` actually dispatches, verifies, pushes, and creates a PR --
    not just discovers and persists."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)
    remote_dir = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote_dir)], check=True, capture_output=True
    )
    _git(repo_dir, "remote", "add", "origin", str(remote_dir))
    _git(repo_dir, "push", "-q", "-u", "origin", "main")

    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        "github:\n  repo: owner/project\nverification:\n  commands:\n    - 'true'\n",
        encoding="utf-8",
    )

    def fake_run_process_group(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        return ProcessExecutionResult(exit_code=0, stdout=_claude_success_stdout(), stderr="")

    monkeypatch.setattr("subsched.agents.claude.run_process_group", fake_run_process_group)
    monkeypatch.setattr(
        "subsched.github.pull_requests.create_or_get_pull_request",
        lambda *a, **k: PullRequestResult(
            kind=PullRequestResultKind.SUCCESS,
            info=PullRequestInfo(
                number=7, url="https://example.invalid/pull/7", title="t", body="b"
            ),
        ),
    )

    result = invoke(
        repo_dir,
        "run",
        "--config",
        str(config_file),
        "--issues",
        "1",
        "--allow-native",
    )

    assert result.exit_code == 0, result.output
    assert "1 issue(s) discovered and persisted" in result.output
    assert "COMPLETE" in result.output

    status = invoke(repo_dir, "status", "--verbose")
    assert "COMPLETE" in status.output
    assert "PR #7" in status.output

