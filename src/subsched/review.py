from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from subsched.models import Task

REQUIRED_REVIEW_SECTIONS = ("## Verdict", "## Summary", "## Findings")


class ReviewVerdict(StrEnum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"


@dataclass(frozen=True, slots=True)
class ReviewReport:
    verdict: ReviewVerdict
    summary: str
    findings: str


def review_report_path(worktree_dir: Path, issue_number: int, round_number: int) -> Path:
    """Path (inside the task worktree, like handoffs/checkpoints) the reviewer Agent is
    instructed to write its structured review report to, and the Scheduler reads back
    from -- see build_review_prompt / read_review_report."""
    return worktree_dir / ".ai" / "reviews" / f"{issue_number}-r{round_number}.md"


def validate_review_content(content: str) -> bool:
    """Validate that the review markdown contains all required sections."""
    if not content.startswith("# Review"):
        return False
    return all(section in content for section in REQUIRED_REVIEW_SECTIONS)


def parse_review_report(content: str) -> ReviewReport | None:
    """Parse a review report file into a structured ReviewReport.

    Fail-closed: returns None (never a default/optimistic verdict) on any schema
    violation -- missing sections, or a Verdict body that isn't exactly one of the two
    known values -- so an ambiguous or malformed reviewer report can never be silently
    treated as an approval.
    """
    if not validate_review_content(content):
        return None

    def get_section(name: str) -> str:
        start = content.find(name)
        if start == -1:
            return ""
        sub = content[start + len(name):].strip()
        next_sec = sub.find("\n## ")
        if next_sec != -1:
            return sub[:next_sec].strip()
        return sub.strip()

    verdict_raw = get_section("## Verdict").strip()
    try:
        verdict = ReviewVerdict(verdict_raw)
    except ValueError:
        return None

    return ReviewReport(
        verdict=verdict,
        summary=get_section("## Summary"),
        findings=get_section("## Findings"),
    )


def read_review_report(
    worktree_dir: Path, issue_number: int, round_number: int
) -> ReviewReport | None:
    """Read back the review report the reviewer Agent was instructed to write.

    Mirrors handoff.py's readback_handoff: the file must exist, must not be a symlink
    (defense against a task worktree pointing the review path somewhere unexpected),
    and must parse to a valid ReviewReport. Any failure returns None so the caller fails
    closed (escalates to NEEDS_HUMAN) rather than guessing a verdict.
    """
    report_file = review_report_path(worktree_dir, issue_number, round_number)
    if report_file.is_symlink() or not report_file.is_file():
        return None
    try:
        content = report_file.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_review_report(content)


def worktree_touched_unexpected_paths(
    worktree_dir: Path,
    issue_number: int,
    round_number: int,
    timeout_seconds: float = 30.0,
) -> bool | None:
    """#281: PR_REVIEW is meant to be strictly read-only, but neither agent CLI's tool
    restrictions are a hard OS-level guarantee against Bash writing files (see
    NativeWorker.run). Enforce it mechanically instead: the only working-tree change a
    reviewer dispatch may make is creating its own review report file. Returns True if
    any other tracked or untracked path changed, False if the change set is clean (only
    the expected review report, or nothing at all), or None if this could not be
    determined at all (git failure) -- callers must fail closed on None, same as
    find_close_keyword_commits elsewhere in this codebase.
    """
    expected = review_report_path(worktree_dir, issue_number, round_number)
    try:
        expected_rel = expected.relative_to(worktree_dir).as_posix()
    except ValueError:
        return True
    try:
        res = subprocess.run(
            ["git", "-C", str(worktree_dir), "status", "--porcelain"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        # `git status --porcelain` lines are "XY path" (or "XY old -> new" for renames);
        # the path always starts at column 4.
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path != expected_rel:
            return True
    return False


def build_pr_comment_body(task: Task, round_number: int, report: ReviewReport) -> str:
    """Format the review report as a PR comment body (posted via gh pr comment)."""
    lines = [
        f"### Automated review (round {round_number}) for issue #{task.issue_number}",
        "",
        f"**Verdict:** {report.verdict.value}",
        "",
        "**Summary**",
        "",
        report.summary or "(none)",
        "",
        "**Findings**",
        "",
        report.findings or "(none)",
    ]
    return "\n".join(lines) + "\n"
