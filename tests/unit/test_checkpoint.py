from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from subsched.checkpoint import (
    MechanicalCheckpoint,
    capture_mechanical_checkpoint,
    load_checkpoint,
    save_checkpoint,
)
from subsched.models import AgentResult, AgentResultKind

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file permission bits are not meaningful on Windows"
)


def _checkpoint(issue_number: int = 130) -> MechanicalCheckpoint:
    return MechanicalCheckpoint(
        issue_number=issue_number,
        git_status="",
        git_diff_stat="",
        changed_files=(),
        head_commit="abc123",
        test_results="pytest passed",
        last_agent_output="did work",
        exit_code=0,
        failure_classification="PASS",
        timestamp="2026-01-01T00:00:00Z",
    )


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


@_POSIX_ONLY
def test_save_checkpoint_hardens_directory_and_file_permissions(tmp_path: Path) -> None:
    """Regression test for #143: checkpoint directory/file must be 0700/0600 even under
    a permissive umask, since checkpoints can contain changed-file lists, test output,
    and agent output summaries other users on the same machine should not read."""
    old_umask = os.umask(0o022)
    try:
        path = save_checkpoint(tmp_path, _checkpoint())
    finally:
        os.umask(old_umask)

    checkpoints_dir = tmp_path / ".ai" / "checkpoints"
    assert stat.S_IMODE(checkpoints_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@_POSIX_ONLY
def test_save_checkpoint_repairs_permissions_of_a_preexisting_checkpoint(
    tmp_path: Path,
) -> None:
    """A checkpoint left behind (0644) by a pre-#143 subsched version must have its
    permissions repaired the next time the directory is touched, without being
    deleted."""
    checkpoints_dir = tmp_path / ".ai" / "checkpoints"
    checkpoints_dir.mkdir(parents=True)
    stale = checkpoints_dir / "999.json"
    stale.write_text('{"issue_number": 999}', encoding="utf-8")
    os.chmod(stale, 0o644)

    save_checkpoint(tmp_path, _checkpoint(issue_number=130))

    assert stale.exists()  # not deleted
    assert stale.read_text(encoding="utf-8") == '{"issue_number": 999}'  # not altered
    assert stat.S_IMODE(stale.stat().st_mode) == 0o600


def test_save_checkpoint_rejects_symlinked_checkpoints_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir()
    (ai_dir / "checkpoints").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="symlink"):
        save_checkpoint(tmp_path, _checkpoint())


def test_save_checkpoint_rejects_symlinked_checkpoint_file(tmp_path: Path) -> None:
    """Regression test for #143 code review: the checkpoints directory itself may be a
    real directory while the specific <issue>.json target has been pre-planted as a
    symlink pointing outside the tree -- this must be rejected fail-closed too, not just
    a symlinked checkpoints directory."""
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_target = outside_dir / "secret.txt"
    outside_target.write_text("do not overwrite me", encoding="utf-8")

    cp = _checkpoint(issue_number=130)
    checkpoints_dir = tmp_path / ".ai" / "checkpoints"
    checkpoints_dir.mkdir(parents=True)
    (checkpoints_dir / f"{cp.issue_number}.json").symlink_to(outside_target)

    with pytest.raises(OSError, match="symlink"):
        save_checkpoint(tmp_path, cp)

    assert outside_target.read_text(encoding="utf-8") == "do not overwrite me"


def test_save_checkpoint_is_atomic_and_readback_succeeds(tmp_path: Path) -> None:
    """No stray .tmp file should survive a successful save, and the checkpoint must be
    immediately readable back with the same content (atomic_write_secure_bytes uses a
    temp file + fsync + os.replace, not a partial in-place write)."""
    cp = _checkpoint()
    target = save_checkpoint(tmp_path, cp)

    checkpoints_dir = tmp_path / ".ai" / "checkpoints"
    leftover_tmp = list(checkpoints_dir.glob("*.tmp"))
    assert leftover_tmp == []

    loaded = load_checkpoint(tmp_path, cp.issue_number)
    assert loaded == cp
    assert target.name == f"{cp.issue_number}.json"
