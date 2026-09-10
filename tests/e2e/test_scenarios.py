from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from subsched.cli import app
from subsched.gitenv import git_safe_env
from subsched.github.issues import GitHubIssueSource
from subsched.github.pull_requests import MergedPrCheckKind, MergedPrCheckResult
from subsched.models import (
    AgentResult,
    AgentResultKind,
    Capacity,
    CapacityState,
    Issue,
    TaskState,
)
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore

runner = CliRunner()
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _assert_isolated_from_primary_repo(path: Path) -> None:
    resolved = path.resolve()
    if resolved == _REPO_ROOT:
        raise AssertionError(f"Refusing git operations on primary repo: {resolved}")
    if _REPO_ROOT in resolved.parents:
        raise AssertionError(f"Refusing git operations inside primary repo: {resolved}")
    if resolved in _REPO_ROOT.parents:
        raise AssertionError(f"Refusing git operations on ancestor of primary repo: {resolved}")


def _init_git_repo(path: Path) -> None:
    _assert_isolated_from_primary_repo(path)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        env=git_safe_env(),
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"],
        check=True,
        capture_output=True,
        env=git_safe_env(),
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
        env=git_safe_env(),
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-q", "-m", "init"],
        check=True,
        capture_output=True,
        env=git_safe_env(),
    )


def invoke(repository: Path, *arguments: str) -> Result:
    return runner.invoke(app, ["--repository", str(repository), *arguments])


def available(agent: str, now: datetime) -> Capacity:
    return Capacity(
        agent=agent,
        state=CapacityState.AVAILABLE,
        used_percentage=10,
        reset_at=now + timedelta(hours=5),
        observed_at=now,
        source="provider",
        confidence="high",
    )


@pytest.fixture(autouse=True)
def _no_real_merged_pr_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "subsched.cli.check_merged_pr_for_issue",
        lambda repo, issue_number, **kwargs: MergedPrCheckResult(kind=MergedPrCheckKind.NONE),
    )
    monkeypatch.setattr("subsched.cli.resolve_default_branch", lambda repo: "main")


@pytest.fixture
def mock_github(monkeypatch: pytest.MonkeyPatch) -> None:
    def list_open(
        self: GitHubIssueSource,
        repo: str,
        *,
        label: str | None = None,
        labels: tuple[str, ...] = (),
        limit: int = 1000,
        **kwargs: object,
    ) -> tuple[Issue, ...]:
        labels_list = ([label] if label else []) + list(labels) + ["ai-ready"]
        active_labels = tuple(dict.fromkeys(labels_list))
        return tuple(
            Issue(number=n, title=f"Scenario Issue {n}", labels=active_labels)
            for n in (10, 20, 30)
        )

    monkeypatch.setattr(GitHubIssueSource, "list_open", list_open)


def test_e2e_multiple_issues_discover_to_complete_lifecycle(tmp_path: Path) -> None:
    """E2E scenario: Discover multiple issues -> dispatch -> verify -> complete.

    Verifies the full forward lifecycle across multiple tasks with ScriptedWorker.
    """
    now = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    issues = (
        Issue(number=10, title="Issue 10"),
        Issue(number=20, title="Issue 20"),
        Issue(number=30, title="Issue 30"),
    )
    worker = ScriptedWorker(
        {
            (10, "claude"): (AgentResult(AgentResultKind.PASS),),
            (20, "claude"): (AgentResult(AgentResultKind.PASS),),
            (30, "claude"): (AgentResult(AgentResultKind.PASS),),
        }
    )
    store = JsonStateStore(tmp_path)
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100), AgentConfig("codex", 90))),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
    )

    # 1. Discover
    scheduler.discover(issues)
    assert len(scheduler.tasks) == 3
    assert all(t.status is TaskState.READY for t in scheduler.tasks)

    # 2. Run to completion
    capacities = (available("claude", now), available("codex", now))
    scheduler.run_until_waiting(capacities, now=now)

    # 3. Assert all completed in correct order
    assert [t.status for t in scheduler.tasks] == [TaskState.COMPLETE] * 3
    assert worker.dispatches == [
        (10, "claude"),
        (20, "claude"),
        (30, "claude"),
    ]

    # 4. Verify durable persistence in storage
    persisted = store.load_tasks()
    assert [t.status for t in persisted] == [TaskState.COMPLETE] * 3
    assert [t.issue_number for t in persisted] == [10, 20, 30]


def test_e2e_abnormal_lifecycle_failover_retry_and_escalation(tmp_path: Path) -> None:
    """E2E scenario: Verify abnormal lifecycle branches:
    - Task 101: Session capacity exhaustion -> failover to alternate agent -> complete
    - Task 102: Generic failure -> retry -> success on second attempt -> complete
    - Task 103: Successive agent failures reaching max_agent_failures -> escalate to NEEDS_HUMAN
    """
    now = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    reset = now + timedelta(hours=2)
    worker = ScriptedWorker(
        {
            # 101: claude exhausted -> switch to codex -> success
            (101, "claude"): (AgentResult(AgentResultKind.CAPACITY_SESSION, reset_at=reset),),
            (101, "codex"): (AgentResult(AgentResultKind.PASS),),
            # 102: codex fails once, succeeds on retry
            (102, "codex"): (
                AgentResult(AgentResultKind.FAILURE),
                AgentResult(AgentResultKind.PASS),
            ),
            # 103: codex fails twice (max_agent_failures=2) -> NEEDS_HUMAN
            (103, "codex"): (
                AgentResult(AgentResultKind.FAILURE),
                AgentResult(AgentResultKind.FAILURE),
            ),
        }
    )
    store = JsonStateStore(tmp_path)
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100), AgentConfig("codex", 90))),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        max_agent_failures=2,
    )
    scheduler.discover(
        (
            Issue(number=101, title="Failover issue"),
            Issue(number=102, title="Retry issue"),
            Issue(number=103, title="Escalation issue"),
        )
    )

    capacities = (available("claude", now), available("codex", now))
    scheduler.run_until_waiting(capacities, now=now)

    # Inspect statuses
    task_map = {t.issue_number: t for t in scheduler.tasks}
    assert task_map[101].status is TaskState.COMPLETE
    assert task_map[101].last_dispatched_agent == "codex"

    assert task_map[102].status is TaskState.COMPLETE
    assert task_map[102].attempt == 1

    assert task_map[103].status is TaskState.NEEDS_HUMAN
    assert task_map[103].attempt == 2
    # Verify failure budget accounting
    failures = dict(task_map[103].per_agent_failures)
    assert failures.get("codex") == 2

    # Verify persisted state matches
    persisted_map = {t.issue_number: t for t in store.load_tasks()}
    assert persisted_map[101].status is TaskState.COMPLETE
    assert persisted_map[102].status is TaskState.COMPLETE
    assert persisted_map[103].status is TaskState.NEEDS_HUMAN


def test_e2e_cli_run_status_metrics_lifecycle(
    tmp_path: Path, mock_github: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E scenario: Verify full CLI workflow from run -> status -> metrics."""
    import shutil

    _init_git_repo(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        """
github:
  repo: owner/project
  mode: label
  include_labels: [ai-ready]
agents:
  claude:
    enabled: true
    priority: 100
  codex:
    enabled: true
    priority: 90
""",
        encoding="utf-8",
    )

    # 1. subsched run --dry-run
    res_run = invoke(tmp_path, "run", "--config", str(config_file), "--dry-run")
    assert res_run.exit_code == 0, res_run.output
    assert "3 issue(s) discovered" in res_run.output
    assert "(dry-run)" in res_run.output

    # 2. subsched status
    res_status = invoke(tmp_path, "status")
    assert res_status.exit_code == 0, res_status.output
    assert "READY" in res_status.output or "Task Queue Status" in res_status.output

    # 3. subsched status --verbose
    res_detail = invoke(tmp_path, "status", "--verbose")
    assert res_detail.exit_code == 0, res_detail.output
    assert "READY" in res_detail.output

    # 4. subsched metrics
    res_metrics = invoke(tmp_path, "metrics")
    assert res_metrics.exit_code == 0, res_metrics.output
    assert "Productivity Metrics" in res_metrics.output

    # 5. subsched metrics --report
    report_file = tmp_path / "metrics_report.md"
    res_report = invoke(tmp_path, "metrics", "--report", str(report_file))
    assert res_report.exit_code == 0, res_report.output
    assert report_file.exists()
    assert "SCHEDULER RUN REPORT" in report_file.read_text(encoding="utf-8")



def test_e2e_scheduler_and_cli_state_coherence(tmp_path: Path) -> None:
    """E2E scenario: Verify that state produced by Scheduler is fully compatible with
    CLI status and metrics reporting.
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    store = JsonStateStore(tmp_path)
    store.init_directories()

    worker = ScriptedWorker(
        {
            (1, "claude"): (AgentResult(AgentResultKind.PASS),),
            (2, "claude"): (AgentResult(AgentResultKind.FAILURE),),
        }
    )
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        max_agent_failures=1,
    )
    scheduler.discover(
        (
            Issue(number=1, title="Resolved issue"),
            Issue(number=2, title="Failed issue"),
        )
    )
    capacities = (available("claude", now),)
    scheduler.run_until_waiting(capacities, now=now)

    # 1 task complete, 1 task needs_human
    assert [t.status for t in scheduler.tasks] == [
        TaskState.COMPLETE,
        TaskState.NEEDS_HUMAN,
    ]

    # Check CLI status against this persisted state
    res_status = invoke(tmp_path, "status")
    assert res_status.exit_code == 0, res_status.output
    assert "COMPLETE" in res_status.output
    assert "NEEDS_HUMAN" in res_status.output

    # Check CLI metrics against this persisted state
    res_metrics = invoke(tmp_path, "metrics")
    assert res_metrics.exit_code == 0, res_metrics.output
    assert "50.0%" in res_metrics.output  # 1 of 2 completed
