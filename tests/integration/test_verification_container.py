"""Opt-in full gate in a real networkless verification container (issue #499)."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from subsched.config import NativeIsolationConfig
from subsched.verification import run_verification


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("linked", [False, True])
def test_real_verification_full_gate_preserves_host_metadata(tmp_path: Path, linked: bool) -> None:
    if os.environ.get("SUBSCHED_VERIFICATION_DOCKER_TEST") != "1":
        pytest.skip("set SUBSCHED_VERIFICATION_DOCKER_TEST=1 for real verification gates")
    runtime = shutil.which("docker")
    image = os.environ.get("SUBSCHED_ISOLATION_WORKER_IMAGE", "")
    assert runtime is not None, "Docker is required for real verification gates"
    assert re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image), "pin worker image by digest"
    repo = tmp_path / "repository"
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-local",
            "--",
            str(Path(__file__).resolve().parents[2]),
            str(repo),
        ],
        check=True,
        capture_output=True,
    )
    sibling = tmp_path / "sibling"
    _git(repo, "worktree", "add", "-q", "-b", "sibling", str(sibling))
    worktree = repo
    if linked:
        worktree = tmp_path / "task"
        _git(repo, "worktree", "add", "-q", "-b", "verification-task", str(worktree))
    _git(repo, "config", "credential.helper", "synthetic-host-helper")
    (repo / ".git/hooks/host-only").write_text("synthetic-host-hook\n")
    (sibling / "canary").write_text("sibling-preserved\n")
    metadata_before = _snapshot(repo / ".git")
    sibling_before = _snapshot(sibling)
    pointer_before = (worktree / ".git").read_bytes() if linked else None
    script = "\n".join(
        [
            'test -z "${GIT_DIR:-}"',
            'test -z "${GIT_WORK_TREE:-}"',
            "git rev-parse --verify HEAD",
            "git status --porcelain",
            "git log -1 --oneline",
            'test -z "$(git config --get credential.helper || true)"',
            "test ! -e /run/subsched-git/hooks/host-only",
            f"test ! -e {shlex.quote(str(sibling))}",
            "test ! -e /var/run/docker.sock",
            "git config verification.canary snapshot-only",
            "git update-ref refs/heads/snapshot-only HEAD",
            "bash scripts/quality_gate.sh",
        ]
    )
    try:
        report = run_verification(
            worktree,
            (shlex.join(("/bin/sh", "-ceu", script)),),
            timeout_seconds=600,
            output_limit_bytes=4 * 1024 * 1024,
            isolation_config=NativeIsolationConfig(backend="container", image=image),
            isolation_runtime_executable=Path(runtime),
        )
        assert report.passed, (
            report.summary + "\n" + "\n".join(gate.stdout + gate.stderr for gate in report.gates)
        )
        assert report.cleanup_confirmed
    finally:
        assert _snapshot(repo / ".git") == metadata_before
        assert _snapshot(sibling) == sibling_before
        if linked:
            assert (worktree / ".git").read_bytes() == pointer_before
