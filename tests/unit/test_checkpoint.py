from __future__ import annotations

import subprocess
from pathlib import Path

from subsched.checkpoint import (
    capture_mechanical_checkpoint,
    load_checkpoint,
    save_checkpoint,
)
from subsched.models import AgentResult, AgentResultKind


def test_capture_save_and_load_mechanical_checkpoint(tmp_path: Path) -> None:
    # Initialize a git repo in tmp_path
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), check=True)
    f1 = tmp_path / "file1.txt"
    f1.write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "file1.txt"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True
    )

    # Modify file
    f1.write_text("hello world", encoding="utf-8")

    result = AgentResult(AgentResultKind.PASS, output="agent did work")
    cp = capture_mechanical_checkpoint(
        worktree_dir=tmp_path,
        issue_number=103,
        agent_result=result,
        exit_code=0,
        test_results="pytest passed",
    )
    assert cp.issue_number == 103
    assert "file1.txt" in cp.changed_files
    assert cp.failure_classification == "PASS"
    assert cp.test_results == "pytest passed"

    path = save_checkpoint(tmp_path, cp)
    assert path.exists()

    loaded = load_checkpoint(tmp_path, 103)
    assert loaded is not None
    assert loaded.issue_number == 103
    assert loaded.changed_files == cp.changed_files
    assert loaded.failure_classification == "PASS"
