from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from subsched.dashboard import api
from subsched.models import Capacity, CapacityState, Task, TaskState
from subsched.storage import JsonStateStore, SchedulerStateSnapshot


def _task(issue: int, status: TaskState = TaskState.IN_PROGRESS, **kwargs: object) -> Task:
    return Task(
        task_id=f"github-{issue}",
        issue_number=issue,
        title=f"Task {issue}",
        labels=(),
        status=status,
        **kwargs,  # type: ignore[arg-type]
    )


def _capacity() -> Capacity:
    return Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=datetime.now(UTC),
        source="test",
        confidence="high",
    )


def test_build_status_counts_and_active_tasks() -> None:
    snapshot = SchedulerStateSnapshot(
        tasks=(
            _task(1, TaskState.IN_PROGRESS),
            _task(2, TaskState.COMPLETE),
            _task(3, TaskState.NEEDS_HUMAN),
        ),
        capacities=(),
        paused=False,
        revision=5,
    )

    result = api.build_status(snapshot)

    assert result["revision"] == 5
    assert result["paused"] is False
    assert result["status_counts"]["IN_PROGRESS"] == 1
    assert result["status_counts"]["COMPLETE"] == 1
    assert result["queue_size"] == 3
    active_issues = {t["issue_number"] for t in result["active_tasks"]}
    assert active_issues == {1, 3}


def test_build_tasks_returns_all_tasks() -> None:
    snapshot = SchedulerStateSnapshot(
        tasks=(_task(1), _task(2)), capacities=(), paused=False, revision=1
    )

    result = api.build_tasks(snapshot)

    assert result["revision"] == 1
    assert {t["issue_number"] for t in result["tasks"]} == {1, 2}


def test_build_task_detail_returns_none_for_unknown_issue() -> None:
    snapshot = SchedulerStateSnapshot(tasks=(_task(1),), capacities=(), paused=False, revision=1)

    assert api.build_task_detail(snapshot, Path("/tmp/does-not-matter"), 999) is None


def test_build_task_detail_redacts_handoff_content(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    handoff_path = store.handoffs_dir / "1.md"
    handoff_path.write_text(
        "# Issue\n#1 Title\n\ngithub_pat_secretsecretsecret\n", encoding="utf-8"
    )

    snapshot = SchedulerStateSnapshot(tasks=(_task(1),), capacities=(), paused=False, revision=1)

    detail = api.build_task_detail(snapshot, tmp_path, 1)

    assert detail is not None
    assert "github_pat_secretsecretsecret" not in detail["handoff"]
    assert "[REDACTED]" in detail["handoff"]


def test_read_handoff_safely_rejects_symlink(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    real_secret = tmp_path / "outside.md"
    real_secret.write_text("secret", encoding="utf-8")
    symlink_path = store.handoffs_dir / "1.md"
    symlink_path.symlink_to(real_secret)

    assert api._read_handoff_safely(tmp_path, 1) is None


def test_read_handoff_safely_returns_none_when_missing(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()

    assert api._read_handoff_safely(tmp_path, 42) is None


def test_read_commit_log_returns_none_without_worktree() -> None:
    assert api._read_commit_log(_task(1)) is None


def test_read_commit_log_returns_none_for_missing_worktree(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert api._read_commit_log(_task(1, worktree=str(missing))) is None


def test_read_commit_log_returns_none_for_symlinked_worktree(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)

    assert api._read_commit_log(_task(1, worktree=str(link))) is None


def test_read_commit_log_returns_redacted_lines(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "a"], cwd=worktree, check=True)
    (worktree / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add ghp_secretsecretsecretsecretsec"],
        cwd=worktree,
        check=True,
    )

    log = api._read_commit_log(_task(1, worktree=str(worktree)))

    assert log is not None
    assert len(log) == 1
    assert "ghp_secretsecretsecretsecretsec" not in log[0]
    assert "[REDACTED]" in log[0]


def test_build_task_detail_includes_commit_log(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "a"], cwd=worktree, check=True)
    (worktree / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=worktree, check=True)

    snapshot = SchedulerStateSnapshot(
        tasks=(_task(1, worktree=str(worktree)),), capacities=(), paused=False, revision=1
    )

    detail = api.build_task_detail(snapshot, tmp_path, 1)

    assert detail is not None
    assert detail["commit_log"] is not None
    assert len(detail["commit_log"]) == 1


def test_build_capacity() -> None:
    snapshot = SchedulerStateSnapshot(
        tasks=(), capacities=(_capacity(),), paused=False, revision=2
    )

    result = api.build_capacity(snapshot)

    assert result["revision"] == 2
    assert result["capacities"][0]["agent"] == "claude"


def test_build_metrics() -> None:
    snapshot = SchedulerStateSnapshot(
        tasks=(_task(1, TaskState.COMPLETE, run_started_at=datetime.now(UTC)),),
        capacities=(),
        paused=False,
        revision=3,
    )

    result = api.build_metrics(snapshot)

    assert result["revision"] == 3
    assert "productivity" in result["metrics"]
    assert "reliability" in result["metrics"]
    assert "capacity" in result["metrics"]
