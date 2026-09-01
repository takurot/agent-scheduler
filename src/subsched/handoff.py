from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from subsched.models import Issue, Task
from subsched.storage import secure_directory

REQUIRED_HANDOFF_SECTIONS = (
    "## Goal",
    "## Current Plan",
    "## Completed",
    "## Current Work",
    "## Decisions",
    "## Known Broken State",
    "## Next Action",
    "## Timestamp",
)


@dataclass(frozen=True, slots=True)
class SemanticHandoff:
    issue_number: int
    title: str
    goal: str
    plan: str
    completed: str
    current_work: str
    decisions: str
    broken_state: str
    next_action: str
    timestamp: str


def validate_handoff_content(content: str) -> bool:
    """Validate that the handoff markdown contains all required sections."""
    if not content.startswith("# Issue"):
        return False
    return all(section in content for section in REQUIRED_HANDOFF_SECTIONS)


def parse_semantic_handoff(content: str) -> SemanticHandoff | None:
    """Parse a handoff file into a structured SemanticHandoff record."""
    if not validate_handoff_content(content):
        return None
    try:
        lines = content.splitlines()
        issue_line = lines[2] if len(lines) > 2 else ""
        match = re.match(r"#(\d+)\s+(.*)", issue_line)
        issue_num = int(match.group(1)) if match else 0
        title = match.group(2) if match else ""

        def get_section(name: str) -> str:
            start = content.find(name)
            if start == -1:
                return ""
            sub = content[start + len(name):].strip()
            next_sec = sub.find("\n## ")
            if next_sec != -1:
                return sub[:next_sec].strip()
            return sub.strip()

        return SemanticHandoff(
            issue_number=issue_num,
            title=title,
            goal=get_section("## Goal"),
            plan=get_section("## Current Plan"),
            completed=get_section("## Completed"),
            current_work=get_section("## Current Work"),
            decisions=get_section("## Decisions"),
            broken_state=get_section("## Known Broken State"),
            next_action=get_section("## Next Action"),
            timestamp=get_section("## Timestamp"),
        )
    except Exception:
        return None


@dataclass(frozen=True, slots=True)
class HandoffReadbackResult:
    """Outcome of validating a handoff file at a worker-end boundary (#145): schema,
    Issue identity, and timestamp advancement since dispatch. `reason` is populated
    (and safe to persist/log -- no raw agent output, no Issue body) whenever `ok` is
    False.
    """

    ok: bool
    reason: str = ""


def _parse_handoff_timestamp(raw_timestamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw_timestamp.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def readback_handoff(
    worktree_dir: Path, task: Task, *, dispatched_at: datetime
) -> HandoffReadbackResult:
    """Validate the handoff file after a worker invocation ends (#145): schema, Issue
    identity, and timestamp advancement since dispatch. Meant to be called at every
    worker-end boundary (normal completion, capacity event, timeout, failure) so
    `handoff.continuous` has an actual runtime-observable meaning instead of being a
    best-effort natural-language instruction the Agent may or may not follow.
    """
    handoff_file = worktree_dir / ".ai" / "handoffs" / f"{task.issue_number}.md"
    if not handoff_file.is_file():
        return HandoffReadbackResult(False, "handoff file missing after worker invocation")
    try:
        content = handoff_file.read_text(encoding="utf-8")
    except OSError as error:
        return HandoffReadbackResult(False, f"handoff file unreadable: {error}")

    parsed = parse_semantic_handoff(content)
    if parsed is None:
        return HandoffReadbackResult(
            False, "handoff schema invalid or missing required sections"
        )
    if parsed.issue_number != task.issue_number:
        return HandoffReadbackResult(
            False,
            f"handoff Issue identity mismatch: expected #{task.issue_number}, "
            f"found #{parsed.issue_number}",
        )

    parsed_ts = _parse_handoff_timestamp(parsed.timestamp)
    if parsed_ts is None:
        return HandoffReadbackResult(
            False, f"handoff timestamp is not a valid ISO 8601 value: {parsed.timestamp!r}"
        )
    if parsed_ts <= dispatched_at:
        return HandoffReadbackResult(
            False,
            f"handoff timestamp ({parsed_ts.isoformat()}) did not advance past dispatch "
            f"time ({dispatched_at.isoformat()})",
        )
    return HandoffReadbackResult(True)


def can_recover_from_checkpoint(
    worktree_dir: Path, task: Task, *, dispatched_at: datetime
) -> bool:
    """A stale/invalid handoff can still be safely continued (#145) if a mechanical
    checkpoint (#23) -- which is captured mechanically by the Scheduler itself, not
    self-reported by the Agent -- proves the same or newer progress happened, so the
    Scheduler is not relying solely on the Agent's own semantic narration.
    """
    from subsched.checkpoint import load_checkpoint

    checkpoint = load_checkpoint(worktree_dir, task.issue_number)
    if checkpoint is None or checkpoint.issue_number != task.issue_number:
        return False
    checkpoint_ts = _parse_handoff_timestamp(checkpoint.timestamp)
    return checkpoint_ts is not None and checkpoint_ts > dispatched_at


def quarantine_corrupt_handoff(handoff_path: Path) -> Path:
    """Move a corrupted handoff file to quarantine with a timestamp."""
    if handoff_path.is_symlink():
        raise OSError(f"refusing to quarantine symlinked handoff: {handoff_path}")
    quarantine_dir = handoff_path.parent / "quarantine"
    secure_directory(quarantine_dir)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    quarantined = quarantine_dir / f"{handoff_path.stem}.corrupt.{ts}.md"
    shutil.move(str(handoff_path), str(quarantined))
    os.chmod(quarantined, 0o600)
    return quarantined


def reconstruct_or_quarantine_handoff(
    worktree_dir: Path, task: Task, issue: Issue | None = None
) -> bool:
    """Decision table for handoff: reconstruct if missing, else quarantine and fail."""
    handoff_file = worktree_dir / ".ai" / "handoffs" / f"{task.issue_number}.md"
    if not handoff_file.exists():
        from subsched.contract import bootstrap_task_files

        bootstrap_task_files(worktree_dir, task, issue)
        return True

    content = handoff_file.read_text(encoding="utf-8")
    if validate_handoff_content(content):
        return True

    quarantine_corrupt_handoff(handoff_file)
    from subsched.contract import bootstrap_task_files

    bootstrap_task_files(worktree_dir, task, issue)
    return False
