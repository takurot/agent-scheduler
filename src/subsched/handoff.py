from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from subsched.models import Issue, Task

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


def quarantine_corrupt_handoff(handoff_path: Path) -> Path:
    """Move a corrupted handoff file to quarantine with a timestamp."""
    quarantine_dir = handoff_path.parent / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    quarantined = quarantine_dir / f"{handoff_path.stem}.corrupt.{ts}.md"
    shutil.move(str(handoff_path), str(quarantined))
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
