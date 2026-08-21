from __future__ import annotations

from pathlib import Path

from subsched.contract import bootstrap_task_files
from subsched.handoff import (
    parse_semantic_handoff,
    quarantine_corrupt_handoff,
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
