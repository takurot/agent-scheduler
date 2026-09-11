from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from subsched import recovery
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


def test_reconcile_with_no_process_record_is_treated_as_crashed(tmp_path: Path) -> None:
    """#164: dispatch always writes a process record before the worker runs; if a
    DISPATCHED/IN_PROGRESS task has none at reconcile time (legacy state, or a crash in
    the narrow window before the record was written), that is unverifiable -- fail-closed
    by treating it exactly like a dead process instead of leaving the task IN_PROGRESS
    and silently stuck forever."""
    issue = Issue(number=103, title="Support timeout")
    task = Task.from_issue(issue)
    task = task.transition(TaskState.DISPATCHED, current_agent="claude")
    task = task.transition(TaskState.IN_PROGRESS, current_agent="claude")
    bootstrap_task_files(tmp_path, task)

    reconciled_task, msg = reconcile_task_recovery(tmp_path, task)
    assert reconciled_task.status is TaskState.RETRY
    assert "no process record" in msg


def test_reconcile_from_dispatched_status_with_missing_worktree_does_not_raise(
    tmp_path: Path,
) -> None:
    """#164: a DISPATCHED (not yet IN_PROGRESS) task escalating to NEEDS_HUMAN must go
    through the state machine's actual allowed transitions -- DISPATCHED cannot jump
    straight to NEEDS_HUMAN -- instead of raising StateTransitionError."""
    issue = Issue(number=104, title="Never started")
    task = Task.from_issue(issue)
    task = task.transition(TaskState.DISPATCHED, current_agent="claude")

    missing_worktree = tmp_path / "does-not-exist"
    reconciled_task, msg = reconcile_task_recovery(missing_worktree, task)
    assert reconciled_task.status is TaskState.NEEDS_HUMAN
    assert reconciled_task.needs_human_reason is not None
    assert "NEEDS_HUMAN" in msg


def test_reconcile_from_in_progress_status_with_missing_worktree_escalates_directly(
    tmp_path: Path,
) -> None:
    """IN_PROGRESS can transition directly to NEEDS_HUMAN (unlike DISPATCHED), so this
    must not go through the extra RETRY hop `_escalate_to_needs_human` uses for
    DISPATCHED."""
    issue = Issue(number=105, title="Already running")
    task = Task.from_issue(issue)
    task = task.transition(TaskState.DISPATCHED, current_agent="claude")
    task = task.transition(TaskState.IN_PROGRESS, current_agent="claude")

    missing_worktree = tmp_path / "does-not-exist"
    reconciled_task, msg = reconcile_task_recovery(missing_worktree, task)
    assert reconciled_task.status is TaskState.NEEDS_HUMAN
    assert reconciled_task.needs_human_reason is not None
    assert "NEEDS_HUMAN" in msg


def test_check_process_liveness_returns_unknown_on_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """os.kill raising a non-ProcessLookupError OSError (e.g. EPERM) means liveness is
    unverifiable, not confirmed dead -- must fail-closed to UNKNOWN rather than DEAD."""

    def fake_kill(pid: int, sig: int) -> None:
        raise PermissionError("no permission to signal pid")

    monkeypatch.setattr(recovery.os, "kill", fake_kill)
    rec = ProcessRecord(
        pid=os.getpid(),
        started_at="2026-08-22T00:00:00Z",
        agent="claude",
        issue_number=103,
        worktree="/irrelevant",
        attempt_nonce="nonce_eperm",
    )
    assert check_process_liveness(rec) is ProcessStatus.UNKNOWN


def test_check_process_liveness_returns_unknown_when_start_time_unverifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a start time was recorded but it can no longer be confirmed (e.g. `ps` failed),
    that must not be treated as confirmation the process is still the original one."""
    monkeypatch.setattr(recovery, "get_process_start_time", lambda pid: None)
    rec = ProcessRecord(
        pid=os.getpid(),
        started_at="2026-08-22T00:00:00Z",
        agent="claude",
        issue_number=103,
        worktree="/irrelevant",
        attempt_nonce="nonce_unverifiable",
    )
    assert check_process_liveness(rec) is ProcessStatus.UNKNOWN


def test_reconcile_with_unknown_liveness_escalates_to_needs_human_and_keeps_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UNKNOWN liveness must not be treated like a dead process: clearing the record and
    retrying could double-run the task if the original process is actually still live."""
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
        attempt_nonce="nonce_unknown",
    )
    save_process_record(tmp_path, rec)

    monkeypatch.setattr(
        recovery, "check_process_liveness", lambda record: ProcessStatus.UNKNOWN
    )
    reconciled_task, msg = reconcile_task_recovery(tmp_path, task)

    assert reconciled_task.status is TaskState.NEEDS_HUMAN
    assert "NEEDS_HUMAN" in msg
    assert load_process_record(tmp_path, 103) is not None


def test_load_process_record_logs_warning_and_returns_none_on_corrupted_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runtime_dir = tmp_path / ".ai" / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "103.process.json").write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="subsched.recovery"):
        result = load_process_record(tmp_path, 103)

    assert result is None
    assert any("corrupted process record" in record.message for record in caplog.records)


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
