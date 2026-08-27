from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from subsched.checkpoint import (
    capture_mechanical_checkpoint,
    load_checkpoint,
    save_checkpoint,
)
from subsched.gitenv import GIT_LOCATION_OVERRIDE_VARS
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


def test_capture_mechanical_checkpoint_strips_git_location_override_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_run_git` must never let a leaked GIT_DIR/GIT_WORK_TREE redirect it away from
    `worktree_dir` (issue #147)."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)

    monkeypatch.setenv("GIT_DIR", "/leaked/.git")
    calls: list[dict[str, object]] = []
    real_run = subprocess.run

    def spying_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", spying_run)

    result = AgentResult(AgentResultKind.PASS, output="agent did work")
    capture_mechanical_checkpoint(
        worktree_dir=tmp_path,
        issue_number=1,
        agent_result=result,
    )

    assert calls, "expected at least one git invocation from capture_mechanical_checkpoint"
    for kwargs in calls:
        env = kwargs.get("env")
        assert isinstance(env, dict), "every git call must pass an explicit env"
        for name in GIT_LOCATION_OVERRIDE_VARS:
            assert name not in env
