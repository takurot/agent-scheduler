from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from subsched.handoff import reconstruct_or_quarantine_handoff
from subsched.models import Task, TaskState


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
    """Check if the recorded PID is currently running and matches."""
    if record.pid <= 0:
        return ProcessStatus.DEAD
    try:
        os.kill(record.pid, 0)
        return ProcessStatus.LIVE
    except ProcessLookupError:
        return ProcessStatus.DEAD
    except OSError:
        return ProcessStatus.UNKNOWN


def reconcile_task_recovery(worktree_dir: Path, task: Task) -> tuple[Task, str]:
    """Reconcile task on recovery: check live/dead process, worktree, and handoff."""
    record = load_process_record(worktree_dir, task.issue_number)
    if record is None:
        return task, "no process record found; state unchanged"

    liveness = check_process_liveness(record)
    if liveness == ProcessStatus.LIVE:
        return task, f"process {record.pid} is still live; resumed monitoring"

    clear_process_record(worktree_dir, task.issue_number)

    if not worktree_dir.exists() or not worktree_dir.is_dir() or worktree_dir.is_symlink():
        return (
            task.transition(TaskState.NEEDS_HUMAN),
            "worktree invalid or missing; escalated to NEEDS_HUMAN",
        )

    handoff_ok = reconstruct_or_quarantine_handoff(worktree_dir, task)
    if not handoff_ok:
        return (
            task.transition(TaskState.NEEDS_HUMAN),
            "handoff was corrupted and quarantined; escalated to NEEDS_HUMAN",
        )

    if task.status in {TaskState.DISPATCHED, TaskState.IN_PROGRESS}:
        return (
            task.transition(TaskState.RETRY),
            f"process {record.pid} terminated; task transitioned to RETRY",
        )

    return task, "process record cleared; task unchanged"
