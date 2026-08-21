from __future__ import annotations

import os
from pathlib import Path

from subsched.contract import bootstrap_task_files
from subsched.models import Issue, Task, TaskState
from subsched.recovery import (
    ProcessRecord,
    clear_process_record,
    load_process_record,
    reconcile_task_recovery,
    save_process_record,
)


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

    rec = ProcessRecord(
        pid=os.getpid(),
        started_at="2026-08-22T00:00:00Z",
        agent="claude",
        issue_number=103,
        worktree=str(tmp_path),
        attempt_nonce="nonce_current",
    )
    save_process_record(tmp_path, rec)

    reconciled_task, msg = reconcile_task_recovery(tmp_path, task)
    assert reconciled_task.status is TaskState.IN_PROGRESS
    assert "resumed monitoring" in msg
