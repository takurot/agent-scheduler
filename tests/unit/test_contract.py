from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from subsched.contract import (
    CONTRACT_BEGIN,
    CONTRACT_END,
    MANAGED_CONTRACT,
    bootstrap_task_files,
    ensure_contract_block,
    validate_contract_in_file,
)
from subsched.models import Issue, Task


def test_managed_contract_forbids_close_keywords_in_commit_messages() -> None:
    """Regression test for #140: the managed AGENTS.md/CLAUDE.md contract block must
    explicitly warn against GitHub auto-close keywords in commit messages, not just
    against pushing/merging/closing the issue directly."""
    assert "Closes" in MANAGED_CONTRACT
    assert "Fixes" in MANAGED_CONTRACT
    assert "Resolves" in MANAGED_CONTRACT
    assert "commit message" in MANAGED_CONTRACT


def test_ensure_contract_block_creates_new_file(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    ensure_contract_block(path)

    assert path.exists()
    assert validate_contract_in_file(path) is True


def test_ensure_contract_block_preserves_custom_instructions(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text(
        "# Project Custom Guidelines\n\nDo not touch legacy database.\n",
        encoding="utf-8",
    )

    ensure_contract_block(path)

    content = path.read_text(encoding="utf-8")
    assert CONTRACT_BEGIN in content
    assert CONTRACT_END in content
    assert "# Project Custom Guidelines" in content
    assert "Do not touch legacy database." in content
    assert validate_contract_in_file(path) is True


def test_ensure_contract_block_replaces_existing_managed_block(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    old_content = f"""# Header

{CONTRACT_BEGIN}
## Old Contract v0
Some old rules
{CONTRACT_END}

# Footer
Footer rules
"""
    path.write_text(old_content, encoding="utf-8")

    ensure_contract_block(path)

    content = path.read_text(encoding="utf-8")
    assert "# Header" in content
    assert "# Footer" in content
    assert "## Old Contract v0" not in content
    assert validate_contract_in_file(path) is True


def test_bootstrap_task_files_creates_task_and_handoff(tmp_path: Path) -> None:
    task = Task.from_issue(
        Issue(number=103, title="Support timeout", body="Timeout handling details")
    )
    bootstrap_task_files(tmp_path, task)

    task_file = tmp_path / ".ai" / "tasks" / "103.md"
    assert task_file.exists()
    assert "Support timeout" in task_file.read_text(encoding="utf-8")
    assert "Timeout handling details" in task_file.read_text(encoding="utf-8")

    handoff_file = tmp_path / ".ai" / "handoffs" / "103.md"
    assert handoff_file.exists()
    assert "#103 Support timeout" in handoff_file.read_text(encoding="utf-8")
    assert "Initial implementation" in handoff_file.read_text(encoding="utf-8")

    assert validate_contract_in_file(tmp_path / "AGENTS.md") is True
    assert validate_contract_in_file(tmp_path / "CLAUDE.md") is True


def test_bootstrap_task_files_preserves_existing_handoff(tmp_path: Path) -> None:
    task = Task.from_issue(
        Issue(number=103, title="Support timeout", body="Timeout handling details")
    )
    handoff_dir = tmp_path / ".ai" / "handoffs"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    existing_handoff = handoff_dir / "103.md"
    existing_handoff.write_text(
        "# Custom Existing Handoff\n\nMade good progress on step 2.\n",
        encoding="utf-8",
    )

    bootstrap_task_files(tmp_path, task)

    content = existing_handoff.read_text(encoding="utf-8")
    assert "# Custom Existing Handoff" in content
    assert "Made good progress on step 2." in content


def test_bootstrap_task_files_stamps_placeholder_with_supplied_now(tmp_path: Path) -> None:
    """Regression test for #145: the initial handoff placeholder must be stamped with
    the caller's logical `now` (the Scheduler's own dispatch clock) when supplied, not
    always real wall-clock time. Otherwise a handoff-freshness readback comparing
    against that same `now` reference is thrown off by the real-time gap between when
    `now` was captured and when this placeholder is actually written -- an untouched
    placeholder could look "fresher than dispatch" purely from that gap, defeating the
    freshness check for a real Agent invocation that never updates the handoff."""
    task = Task.from_issue(Issue(number=103, title="Support timeout"))
    supplied_now = datetime(2030, 1, 1, tzinfo=UTC)

    bootstrap_task_files(tmp_path, task, now=supplied_now)

    handoff_file = tmp_path / ".ai" / "handoffs" / "103.md"
    assert supplied_now.isoformat() in handoff_file.read_text(encoding="utf-8")
