from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("component", [".ai", "handoffs", "1.md"])
@pytest.mark.parametrize("swap_after_open", [False, True])
def test_read_handoff_pins_components(tmp_path: Path, monkeypatch, component, swap_after_open):
    repository = tmp_path / "repo"
    repository.mkdir()
    store = JsonStateStore(repository)
    store.init_directories()
    (store.handoffs_dir / "1.md").write_text("safe handoff", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "handoffs").mkdir()
    for directory in (outside, outside / "handoffs"):
        (directory / "1.md").write_text("OUTSIDE_HANDOFF_CANARY", encoding="utf-8")
    target = {
        ".ai": repository / ".ai",
        "handoffs": store.handoffs_dir,
        "1.md": store.handoffs_dir / "1.md",
    }[component]
    original_open = os.open
    swapped = False

    def swap():
        nonlocal swapped
        target.rename(target.with_name(target.name + ".saved"))
        target.symlink_to(outside / "1.md" if component == "1.md" else outside)
        swapped = True

    def racing_open(path, flags, *args, **kwargs):
        matches = path == component and not swapped
        if matches and not swap_after_open:
            swap()
        descriptor = original_open(path, flags, *args, **kwargs)
        if matches and swap_after_open:
            swap()
        return descriptor

    monkeypatch.setattr(api.os, "open", racing_open)
    result = api._read_handoff_safely(repository, 1)
    assert swapped
    assert result == ("safe handoff" if swap_after_open else None)


@pytest.mark.parametrize("kind", ["directory", "fifo", "invalid_utf8"])
def test_read_handoff_rejects_non_documents(tmp_path: Path, kind: str):
    store = JsonStateStore(tmp_path)
    store.init_directories()
    path = store.handoffs_dir / "1.md"
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"\xff")
    assert api._read_handoff_safely(tmp_path, 1) is None


def test_read_handoff_fails_closed_without_nofollow(tmp_path: Path, monkeypatch):
    store = JsonStateStore(tmp_path)
    store.init_directories()
    (store.handoffs_dir / "1.md").write_text("safe handoff", encoding="utf-8")
    monkeypatch.delattr(api.os, "O_NOFOLLOW")
    assert api._read_handoff_safely(tmp_path, 1) is None


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
