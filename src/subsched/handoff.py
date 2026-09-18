from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.models import Issue, Task
from subsched.storage import atomic_write_secure_bytes, secure_directory

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
    repaired: bool = False


def _parse_handoff_timestamp(raw_timestamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw_timestamp.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def strip_handoff_timestamp(content: str) -> str:
    """Return handoff content with the ## Timestamp section removed,
    for comparing substantive content changes."""
    pattern = re.compile(r"(##\s+Timestamp\s*\n+)(.*?)(?=(\n## |\Z))", re.DOTALL)
    return pattern.sub("", content).strip()


def compute_substantive_handoff_hash(content: str) -> str:
    """Compute sha256 hash of handoff content excluding the ## Timestamp section."""
    substantive = strip_handoff_timestamp(content)
    return hashlib.sha256(substantive.encode("utf-8")).hexdigest()


def replace_handoff_timestamp(content: str, new_timestamp: str) -> str:
    """Replace the ## Timestamp section content with new_timestamp."""
    pattern = re.compile(r"(##\s+Timestamp\s*\n+)(.*?)(?=(\n## |\Z))", re.DOTALL)
    if pattern.search(content):
        return pattern.sub(lambda m: f"{m.group(1)}{new_timestamp}\n", content)
    return f"{content.rstrip()}\n\n## Timestamp\n\n{new_timestamp}\n"


def readback_handoff(
    worktree_dir: Path,
    task: Task,
    *,
    dispatched_at: datetime,
    pre_dispatch_handoff_hash: str | None = None,
    pre_dispatch_head: str | None = None,
    current_head: str | None = None,
    auto_repair: bool = True,
    now: datetime | None = None,
) -> HandoffReadbackResult:
    """Validate the handoff file after a worker invocation ends (#145): schema, Issue
    identity, and timestamp advancement since dispatch. Meant to be called at every
    worker-end boundary (normal completion, capacity event, timeout, failure) so
    `handoff.continuous` has an actual runtime-observable meaning instead of being a
    best-effort natural-language instruction the Agent may or may not follow.

    If the agent omitted advancing ## Timestamp or wrote a malformed timestamp, but
    demonstrated substantive progress (#365) via content changes or new git commits,
    the timestamp is auto-repaired to `now` and accepted instead of escalating to
    NEEDS_HUMAN.
    """
    handoff_file = worktree_dir / ".ai" / "handoffs" / f"{task.issue_number}.md"
    if not handoff_file.is_file() or handoff_file.is_symlink():
        return HandoffReadbackResult(
            False, "handoff file missing or symlink after worker invocation"
        )
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
    if parsed_ts is not None and parsed_ts > dispatched_at:
        return HandoffReadbackResult(True)

    # #365: Check for substantive progress (content hash change or new git commit)
    has_content_progress = False
    if pre_dispatch_handoff_hash is not None:
        current_hash = compute_substantive_handoff_hash(content)
        if current_hash != pre_dispatch_handoff_hash:
            has_content_progress = True

    has_commit_progress = False
    if pre_dispatch_head is not None:
        if current_head is None:
            from subsched.review import git_head_commit

            current_head = git_head_commit(worktree_dir)
        if current_head is not None and current_head != pre_dispatch_head:
            has_commit_progress = True

    if (has_content_progress or has_commit_progress) and auto_repair:
        effective_now = now or datetime.now(UTC)
        if effective_now <= dispatched_at:
            effective_now = dispatched_at + timedelta(seconds=1)
        repair_ts_str = effective_now.strftime("%Y-%m-%dT%H:%M:%SZ")
        repaired_content = replace_handoff_timestamp(content, repair_ts_str)
        try:
            atomic_write_secure_bytes(handoff_file, repaired_content.encode("utf-8"))
        except OSError as error:
            return HandoffReadbackResult(
                False, f"failed to auto-repair handoff timestamp: {error}"
            )
        return HandoffReadbackResult(True, repaired=True)

    # Fail closed on true stale handoff (#145)
    if parsed_ts is None:
        return HandoffReadbackResult(
            False, f"handoff timestamp is not a valid ISO 8601 value: {parsed.timestamp!r}"
        )
    return HandoffReadbackResult(
        False,
        f"handoff timestamp ({parsed_ts.isoformat()}) did not advance past dispatch "
        f"time ({dispatched_at.isoformat()})",
    )



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
