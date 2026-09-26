from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from subsched.gitenv import git_safe_env
from subsched.metrics import calculate_metrics
from subsched.models import Task, TaskState
from subsched.storage import SchedulerStateSnapshot
from subsched.structured_logger import redact_sensitive_text

# Tasks in any state other than these are considered "active" for the dashboard's
# at-a-glance summary -- everything still in flight or blocked on an operator.
_TERMINAL_STATES = frozenset({TaskState.COMPLETE, TaskState.FAILED, TaskState.CANCELLED})


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _task_summary(task: Task) -> dict[str, Any]:
    result: dict[str, Any] = _redact(task.to_dict())
    return result


def build_status(snapshot: SchedulerStateSnapshot) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for task in snapshot.tasks:
        counts[task.status.value] = counts.get(task.status.value, 0) + 1
    active = [_task_summary(t) for t in snapshot.tasks if t.status not in _TERMINAL_STATES]
    return {
        "paused": snapshot.paused,
        "revision": snapshot.revision,
        "status_counts": counts,
        "active_tasks": active,
        "queue_size": len(snapshot.tasks),
    }


def build_tasks(snapshot: SchedulerStateSnapshot) -> dict[str, Any]:
    return {
        "revision": snapshot.revision,
        "tasks": [_task_summary(t) for t in snapshot.tasks],
    }


def _read_handoff_safely(repository: Path, issue: int) -> str | None:
    """Read `.ai/handoffs/{issue}.md`, refusing a symlinked handoff file or one whose
    resolved path escapes the handoffs directory (fail-closed against path traversal,
    mirroring `storage.py`'s symlink/commonpath checks).
    """
    handoffs_dir = (repository / ".ai" / "handoffs").resolve()
    handoff_path = handoffs_dir / f"{issue}.md"
    if handoff_path.is_symlink() or not handoff_path.is_file():
        return None
    resolved = handoff_path.resolve()
    if os.path.commonpath([str(resolved), str(handoffs_dir)]) != str(handoffs_dir):
        return None
    try:
        return handoff_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _read_commit_log(task: Task) -> list[str] | None:
    if not task.worktree:
        return None
    worktree = Path(task.worktree)
    if worktree.is_symlink() or not worktree.is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "log", "-n", "10", "--oneline"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=git_safe_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [redact_sensitive_text(line) for line in result.stdout.splitlines()]


def build_task_detail(
    snapshot: SchedulerStateSnapshot, repository: Path, issue: int
) -> dict[str, Any] | None:
    matches = [t for t in snapshot.tasks if t.issue_number == issue]
    if not matches:
        return None
    task = matches[0]
    detail = _task_summary(task)
    handoff = _read_handoff_safely(repository, issue)
    detail["handoff"] = _redact(handoff) if handoff is not None else None
    detail["commit_log"] = _read_commit_log(task)
    return detail


def build_capacity(snapshot: SchedulerStateSnapshot) -> dict[str, Any]:
    return {
        "revision": snapshot.revision,
        "capacities": [_redact(c.to_dict()) for c in snapshot.capacities],
    }


def build_metrics(snapshot: SchedulerStateSnapshot) -> dict[str, Any]:
    metrics = calculate_metrics(snapshot.tasks)
    return {
        "revision": snapshot.revision,
        "metrics": _redact(metrics.to_dict()),
    }
