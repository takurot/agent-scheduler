from __future__ import annotations

import os
from pathlib import Path

from subsched.contract import bootstrap_task_files
from subsched.models import Issue, Task, TaskState
from subsched.recovery import (
    ProcessRecord,
    ProcessStatus,
    check_process_liveness,
    clear_process_record,
    load_process_record,
    reconcile_task_recovery,
    save_process_record,
)
from subsched.storage import get_process_start_time


def test_process_record_save_load_clear(tmp_path: Path) -> None:
    rec = ProcessRecord(
        pid=12345,
        started_at="2026-08-22T00:00:00Z",
        agent="claude",
        issue_number=103,
        worktree=str(tmp_path),
        attempt_nonce="nonce_abc",
    )
    p = save_process_record(tmp_path, rec)
    assert p.exists()

    loaded = load_process_record(tmp_path, 103)
    assert loaded is not None
    assert loaded.pid == 12345
    assert loaded.attempt_nonce == "nonce_abc"

    clear_process_record(tmp_path, 103)
    assert load_process_record(tmp_path, 103) is None


def test_reconcile_with_dead_process_and_valid_worktree(tmp_path: Path) -> None:
    issue = Issue(number=103, title="Support timeout")
    task = Task.from_issue(issue)
    task = task.transition(TaskState.DISPATCHED, current_agent="claude")
    task = task.transition(TaskState.IN_PROGRESS, current_agent="claude")
    bootstrap_task_files(tmp_path, task)

    rec = ProcessRecord(
        pid=999999,
        started_at="2026-08-22T00:00:00Z",
        agent="claude",
        issue_number=103,
        worktree=str(tmp_path),
        attempt_nonce="nonce_123",
    )
    save_process_record(tmp_path, rec)

    reconciled_task, msg = reconcile_task_recovery(tmp_path, task)
    assert reconciled_task.status is TaskState.RETRY
    assert "RETRY" in msg


def test_reconcile_with_live_process(tmp_path: Path) -> None:
    issue = Issue(number=103, title="Support timeout")
    task = Task.from_issue(issue)
    task = task.transition(TaskState.DISPATCHED, current_agent="claude")
    task = task.transition(TaskState.IN_PROGRESS, current_agent="claude")
    bootstrap_task_files(tmp_path, task)

    actual_start_time = get_process_start_time(os.getpid()) or ""
    rec = ProcessRecord(
        pid=os.getpid(),
        started_at=actual_start_time,
        agent="claude",
        issue_number=103,
        worktree=str(tmp_path),
        attempt_nonce="nonce_current",
    )
    save_process_record(tmp_path, rec)

    reconciled_task, msg = reconcile_task_recovery(tmp_path, task)
    assert reconciled_task.status is TaskState.IN_PROGRESS
    assert "resumed monitoring" in msg


def test_reconcile_treats_pid_reuse_as_dead_not_live(tmp_path: Path) -> None:
    """A live PID whose recorded start time no longer matches the running process (i.e. the
    OS reassigned the PID to an unrelated process) must not be reported as the original
    worker still being live."""
    issue = Issue(number=103, title="Support timeout")
    task = Task.from_issue(issue)
    task = task.transition(TaskState.DISPATCHED, current_agent="claude")
    task = task.transition(TaskState.IN_PROGRESS, current_agent="claude")
    bootstrap_task_files(tmp_path, task)

    rec = ProcessRecord(
        pid=os.getpid(),
        started_at="Mon Jan  1 00:00:00 1990",
        agent="claude",
        issue_number=103,
        worktree=str(tmp_path),
        attempt_nonce="nonce_stale",
    )
    save_process_record(tmp_path, rec)

    reconciled_task, msg = reconcile_task_recovery(tmp_path, task)
    assert reconciled_task.status is TaskState.RETRY
    assert "RETRY" in msg
    assert load_process_record(tmp_path, 103) is None


def test_check_process_liveness_detects_pid_reuse() -> None:
    rec = ProcessRecord(
        pid=os.getpid(),
        started_at="Mon Jan  1 00:00:00 1990",
        agent="claude",
        issue_number=103,
        worktree="/irrelevant",
        attempt_nonce="nonce_stale",
    )
    assert check_process_liveness(rec) is ProcessStatus.DEAD


def test_check_process_liveness_matches_real_start_time() -> None:
    actual_start_time = get_process_start_time(os.getpid()) or ""
    rec = ProcessRecord(
        pid=os.getpid(),
        started_at=actual_start_time,
        agent="claude",
        issue_number=103,
        worktree="/irrelevant",
        attempt_nonce="nonce_current",
    )
    assert check_process_liveness(rec) is ProcessStatus.LIVE
