from __future__ import annotations

import contextlib
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from subsched.agents.process import redact_sensitive_command_audit
from subsched.gitenv import git_safe_env
from subsched.models import ALLOWED_TRANSITIONS, Task, TaskState


class RebaseStatus(StrEnum):
    SUCCESS = "SUCCESS"
    CONFLICT = "CONFLICT"
    ALREADY_UP_TO_DATE = "ALREADY_UP_TO_DATE"
    FAILURE = "FAILURE"


@dataclass(frozen=True, slots=True)
class RebaseResult:
    status: RebaseStatus
    conflicted_files: tuple[str, ...] = ()
    output: str = ""


def rebase_onto_base(
    worktree_dir: Path,
    base_branch: str = "main",
    timeout_seconds: float = 60.0,
) -> RebaseResult:
    """Attempt rebasing worktree branch onto base branch, aborting cleanly on conflict."""
    try:
        proc = subprocess.run(
            ["git", "rebase", base_branch],
            cwd=str(worktree_dir),
            env=git_safe_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return RebaseResult(
            status=RebaseStatus.FAILURE,
            output=f"rebase execution failed: {err}",
        )

    output = f"{proc.stdout}\n{proc.stderr}"
    redacted = redact_sensitive_command_audit(tuple(output.splitlines()))
    clean_output = "\n".join(redacted)

    if proc.returncode == 0:
        return RebaseResult(status=RebaseStatus.SUCCESS, output=clean_output)

    # Check for conflicted files
    conflicted_files: list[str] = []
    with contextlib.suppress(Exception):
        diff_proc = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=str(worktree_dir),
            env=git_safe_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10.0,
            check=False,
        )
        if diff_proc.returncode == 0 and diff_proc.stdout.strip():
            conflicted_files = [f.strip() for f in diff_proc.stdout.splitlines() if f.strip()]

    # Always abort rebase to leave worktree in a clean recoverable state
    with contextlib.suppress(Exception):
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=str(worktree_dir),
            env=git_safe_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )

    if conflicted_files or "conflict" in clean_output.casefold():
        return RebaseResult(
            status=RebaseStatus.CONFLICT,
            conflicted_files=tuple(conflicted_files),
            output=clean_output,
        )

    return RebaseResult(status=RebaseStatus.FAILURE, output=clean_output)


def handle_rebase_outcome(task: Task, result: RebaseResult) -> tuple[Task, str]:
    """Transition task according to rebase outcome."""
    if result.status in {RebaseStatus.SUCCESS, RebaseStatus.ALREADY_UP_TO_DATE}:
        if task.status == TaskState.NEEDS_REBASE:
            return (
                task.transition(TaskState.READY),
                "rebase completed cleanly; transitioned to READY",
            )
        return task, "rebase completed cleanly"
    elif result.status == RebaseStatus.CONFLICT:
        files = list(result.conflicted_files)
        msg = f"merge conflict detected in {files}; escalated to NEEDS_HUMAN"
        if TaskState.NEEDS_HUMAN in ALLOWED_TRANSITIONS.get(task.status, frozenset()):
            return task.transition(TaskState.NEEDS_HUMAN, reason=msg), msg
        if task.status == TaskState.IN_PROGRESS:
            verifying = task.transition(TaskState.VERIFYING)
            return verifying.transition(TaskState.NEEDS_HUMAN, reason=msg), msg
        if task.status == TaskState.FAILED:
            return task, msg
        return task.transition(TaskState.FAILED), msg
    else:
        msg = f"rebase failed: {result.output}; escalated to NEEDS_HUMAN"
        if TaskState.NEEDS_HUMAN in ALLOWED_TRANSITIONS.get(task.status, frozenset()):
            return task.transition(TaskState.NEEDS_HUMAN, reason=msg), msg
        if task.status == TaskState.IN_PROGRESS:
            verifying = task.transition(TaskState.VERIFYING)
            return verifying.transition(TaskState.NEEDS_HUMAN, reason=msg), msg
        if task.status == TaskState.FAILED:
            return task, msg
        return task.transition(TaskState.FAILED), msg
