from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.checkpoint import MechanicalCheckpoint, save_checkpoint
from subsched.contract import bootstrap_task_files
from subsched.handoff import (
    can_recover_from_checkpoint,
    parse_semantic_handoff,
    quarantine_corrupt_handoff,
    readback_handoff,
    reconstruct_or_quarantine_handoff,
    validate_handoff_content,
)
from subsched.models import Issue, Task


def test_validate_and_parse_valid_handoff(tmp_path: Path) -> None:
    task = Task.from_issue(Issue(number=103, title="Support timeout"))
    bootstrap_task_files(tmp_path, task)
    handoff_path = tmp_path / ".ai" / "handoffs" / "103.md"
    content = handoff_path.read_text(encoding="utf-8")

    assert validate_handoff_content(content) is True
    parsed = parse_semantic_handoff(content)
    assert parsed is not None
    assert parsed.issue_number == 103
    assert parsed.title == "Support timeout"
    assert "Support timeout" in parsed.goal


def test_quarantine_corrupt_handoff(tmp_path: Path) -> None:
    handoff_dir = tmp_path / ".ai" / "handoffs"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    corrupt_file = handoff_dir / "103.md"
    corrupt_file.write_text("Corrupted garbage without sections", encoding="utf-8")

    assert validate_handoff_content(corrupt_file.read_text(encoding="utf-8")) is False
    quarantined = quarantine_corrupt_handoff(corrupt_file)
    assert not corrupt_file.exists()
    assert quarantined.exists()
    assert "quarantine" in str(quarantined)


def _write_handoff(worktree_dir: Path, issue_number: int, *, title: str, timestamp: str) -> None:
    handoffs_dir = worktree_dir / ".ai" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    (handoffs_dir / f"{issue_number}.md").write_text(
        f"""# Issue

#{issue_number} {title}

## Goal

{title}

## Current Plan

- Plan

## Completed

- Did real work

## Current Work

Wrapping up

## Decisions

- Used approach X

## Known Broken State

- None

## Next Action

- Verify

## Timestamp

{timestamp}
""",
        encoding="utf-8",
    )


def test_readback_handoff_accepts_freshly_advanced_handoff(tmp_path: Path) -> None:
    """Regression test for #145: a handoff whose timestamp genuinely advanced past
    dispatch time, with valid schema and matching Issue identity, must pass readback."""
    task = Task.from_issue(Issue(number=103, title="Support timeout"))
    dispatched_at = datetime(2026, 1, 1, tzinfo=UTC)
    _write_handoff(
        tmp_path,
        103,
        title="Support timeout",
        timestamp=(dispatched_at + timedelta(minutes=5)).isoformat(),
    )

    result = readback_handoff(tmp_path, task, dispatched_at=dispatched_at)
    assert result.ok is True


def test_readback_handoff_rejects_missing_file(tmp_path: Path) -> None:
    task = Task.from_issue(Issue(number=103, title="Support timeout"))
    result = readback_handoff(tmp_path, task, dispatched_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert result.ok is False
    assert "missing" in result.reason


def test_readback_handoff_rejects_stale_timestamp(tmp_path: Path) -> None:
    """The dogfooded #130 failure mode: the handoff still shows the bootstrap
    placeholder (or any timestamp that didn't advance) after ~4 minutes of real
    worker.run() -- must be rejected, not silently trusted."""
    task = Task.from_issue(Issue(number=103, title="Support timeout"))
    dispatched_at = datetime(2026, 1, 1, tzinfo=UTC)
    _write_handoff(
        tmp_path,
        103,
        title="Support timeout",
        # Written before dispatch (or exactly at dispatch) -- did not advance.
        timestamp=dispatched_at.isoformat(),
    )

    result = readback_handoff(tmp_path, task, dispatched_at=dispatched_at)
    assert result.ok is False
    assert "did not advance" in result.reason


def test_readback_handoff_rejects_issue_identity_mismatch(tmp_path: Path) -> None:
    task = Task.from_issue(Issue(number=103, title="Support timeout"))
    dispatched_at = datetime(2026, 1, 1, tzinfo=UTC)
    handoffs_dir = tmp_path / ".ai" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    # Handoff file is at the right path but its own content claims a different Issue --
    # e.g. accidentally copied from another task's worktree.
    _write_handoff(
        tmp_path,
        999,
        title="Wrong issue",
        timestamp=(dispatched_at + timedelta(minutes=1)).isoformat(),
    )
    (handoffs_dir / "103.md").write_text(
        (handoffs_dir / "999.md").read_text(encoding="utf-8"), encoding="utf-8"
    )

    result = readback_handoff(tmp_path, task, dispatched_at=dispatched_at)
    assert result.ok is False
    assert "identity mismatch" in result.reason


def test_readback_handoff_rejects_invalid_schema(tmp_path: Path) -> None:
    task = Task.from_issue(Issue(number=103, title="Support timeout"))
    handoffs_dir = tmp_path / ".ai" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    (handoffs_dir / "103.md").write_text("not a real handoff", encoding="utf-8")

    result = readback_handoff(tmp_path, task, dispatched_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert result.ok is False
    assert "schema" in result.reason


def test_can_recover_from_checkpoint_when_checkpoint_is_fresh(tmp_path: Path) -> None:
    """#145: a stale/invalid handoff can still be safely continued if a mechanical
    checkpoint -- captured by the Scheduler itself, not self-reported by the Agent --
    proves newer progress happened."""
    task = Task.from_issue(Issue(number=103, title="Support timeout"))
    dispatched_at = datetime(2026, 1, 1, tzinfo=UTC)
    cp = MechanicalCheckpoint(
        issue_number=103,
        git_status="",
        git_diff_stat="",
        changed_files=(),
        head_commit="abc123",
        test_results="",
        last_agent_output="",
        exit_code=0,
        failure_classification="PASS",
        timestamp=(dispatched_at + timedelta(minutes=2)).isoformat(),
    )
    save_checkpoint(tmp_path, cp)

    assert can_recover_from_checkpoint(tmp_path, task, dispatched_at=dispatched_at) is True


def test_can_recover_from_checkpoint_rejects_stale_or_missing_checkpoint(
    tmp_path: Path,
) -> None:
    task = Task.from_issue(Issue(number=103, title="Support timeout"))
    dispatched_at = datetime(2026, 1, 1, tzinfo=UTC)

    # No checkpoint at all.
    assert can_recover_from_checkpoint(tmp_path, task, dispatched_at=dispatched_at) is False

    # A checkpoint that predates dispatch does not prove anything happened this attempt.
    cp = MechanicalCheckpoint(
        issue_number=103,
        git_status="",
        git_diff_stat="",
        changed_files=(),
        head_commit="abc123",
        test_results="",
        last_agent_output="",
        exit_code=0,
        failure_classification="PASS",
        timestamp=(dispatched_at - timedelta(minutes=2)).isoformat(),
    )
    save_checkpoint(tmp_path, cp)
    assert can_recover_from_checkpoint(tmp_path, task, dispatched_at=dispatched_at) is False


def test_reconstruct_decision_table(tmp_path: Path) -> None:
    task = Task.from_issue(Issue(number=103, title="Support timeout"))
    res1 = reconstruct_or_quarantine_handoff(tmp_path, task)
    assert res1 is True
    assert (tmp_path / ".ai" / "handoffs" / "103.md").exists()

    # Corrupt handoff -> Quarantined, returns False
    handoff_file = tmp_path / ".ai" / "handoffs" / "103.md"
    handoff_file.write_text("Corrupt content", encoding="utf-8")
    res2 = reconstruct_or_quarantine_handoff(tmp_path, task)
    assert res2 is False
    assert (tmp_path / ".ai" / "handoffs" / "quarantine").exists()
