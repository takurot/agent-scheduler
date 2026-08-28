from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from subsched.handoff import reconstruct_or_quarantine_handoff
from subsched.models import Task, TaskState
from subsched.storage import get_process_start_time


class ProcessStatus(StrEnum):
    LIVE = "LIVE"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    pid: int
    started_at: str
    agent: str
    issue_number: int
    worktree: str
    attempt_nonce: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ProcessRecord:
        return cls(
            pid=int(str(data.get("pid", 0))),
            started_at=str(data.get("started_at", "")),
            agent=str(data.get("agent", "")),
            issue_number=int(str(data.get("issue_number", data.get("issue", 0)))),
            worktree=str(data.get("worktree", "")),
            attempt_nonce=str(data.get("attempt_nonce", "")),
        )


def save_process_record(worktree_dir: Path, record: ProcessRecord) -> Path:
    """Persist process record under .ai/runtime/<issue>.process.json."""
    runtime_dir = worktree_dir / ".ai" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target = runtime_dir / f"{record.issue_number}.process.json"
    temp_target = runtime_dir / f"{record.issue_number}.process.json.tmp"
    data = json.dumps(record.to_dict(), indent=2)
    temp_target.write_text(data, encoding="utf-8")
    temp_target.replace(target)
    return target


def load_process_record(worktree_dir: Path, issue_number: int) -> ProcessRecord | None:
    """Load persisted process record for an issue."""
    target = worktree_dir / ".ai" / "runtime" / f"{issue_number}.process.json"
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return ProcessRecord.from_dict(data)
    except Exception:
        return None


def clear_process_record(worktree_dir: Path, issue_number: int) -> None:
    """Remove process record after normal or terminal completion."""
    target = worktree_dir / ".ai" / "runtime" / f"{issue_number}.process.json"
    if target.is_file():
        target.unlink(missing_ok=True)


def check_process_liveness(record: ProcessRecord) -> ProcessStatus:
    """Check if the recorded PID is currently running and still the same process.

    Matches the recorded pid against its process start time (the same PID + start-time
    comparison storage.is_process_alive uses) so that OS PID reuse cannot be mistaken for
    the original worker still being live: if `record.pid` has since been reassigned to an
    unrelated process, this returns DEAD instead of LIVE.
    """
    if record.pid <= 0:
        return ProcessStatus.DEAD
    try:
        os.kill(record.pid, 0)
    except ProcessLookupError:
        return ProcessStatus.DEAD
    except OSError:
        return ProcessStatus.UNKNOWN

    if record.started_at:
        actual_start_time = get_process_start_time(record.pid)
        if actual_start_time is not None and actual_start_time != record.started_at:
            return ProcessStatus.DEAD

    return ProcessStatus.LIVE


def escalate_to_needs_human(task: Task, reason: str) -> Task:
    """Transition to NEEDS_HUMAN via the state machine's actual allowed edges.

    DISPATCHED cannot transition directly to NEEDS_HUMAN (see ALLOWED_TRANSITIONS in
    models.py) -- only IN_PROGRESS can. A crash-recovery escalation must still succeed
    for a task that crashed before ever reaching IN_PROGRESS, so route it through RETRY
    first when necessary instead of raising StateTransitionError.
    """
    if task.status is TaskState.DISPATCHED:
        task = task.transition(TaskState.RETRY, reason=reason)
    return task.transition(TaskState.NEEDS_HUMAN, reason=reason)


def reconcile_task_recovery(worktree_dir: Path, task: Task) -> tuple[Task, str]:
    """Reconcile task on recovery: check live/dead process, worktree, and handoff."""
    record = load_process_record(worktree_dir, task.issue_number)
    if record is not None:
        liveness = check_process_liveness(record)
        if liveness == ProcessStatus.LIVE:
            return task, f"process {record.pid} is still live; resumed monitoring"
        clear_process_record(worktree_dir, task.issue_number)
        reason_prefix = f"process {record.pid} terminated"
    else:
        # #164: dispatch always writes a process record before the worker runs, so a
        # DISPATCHED/IN_PROGRESS task with none here is unverifiable (legacy state from
        # before this record existed, or a crash in the narrow window before it was
        # written). Fail-closed by treating it the same as a confirmed-dead process
        # rather than leaving the task's state unchanged and permanently stuck.
        reason_prefix = "no process record found for in-flight task"

    if not worktree_dir.exists() or not worktree_dir.is_dir() or worktree_dir.is_symlink():
        reason = f"{reason_prefix}; worktree invalid or missing; escalated to NEEDS_HUMAN"
        return escalate_to_needs_human(task, reason), reason

    handoff_ok = reconstruct_or_quarantine_handoff(worktree_dir, task)
    if not handoff_ok:
        reason = f"{reason_prefix}; handoff was corrupted and quarantined; escalated to NEEDS_HUMAN"
        return escalate_to_needs_human(task, reason), reason

    if task.status in {TaskState.DISPATCHED, TaskState.IN_PROGRESS}:
        reason = f"{reason_prefix}; task transitioned to RETRY"
        # #164: counts the same as a live AgentResultKind.FAILURE (see
        # Scheduler._handle_result's generic failure path) so a crash-loop -- the
        # process dying repeatedly on the same task -- still escalates to NEEDS_HUMAN
        # via the existing max_agent_failures budget instead of respawning forever.
        return (
            task.transition(TaskState.RETRY, reason=reason, increment_attempt=True),
            reason,
        )

    return task, f"{reason_prefix}; process record cleared; task unchanged"
