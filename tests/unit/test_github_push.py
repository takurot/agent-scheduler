from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from subsched.github.push import (
    PushResultKind,
    push_task_branch,
    validate_branch_name,
    validate_remote_name,
)


def test_validate_branch_and_remote_name() -> None:
    assert validate_branch_name("main") is False
    assert validate_branch_name("master") is False
    assert validate_branch_name("release") is False
    assert validate_branch_name("subsched/issue-103") is True
    assert validate_branch_name("issue/103-fix-bug") is True

    assert validate_remote_name("origin") is True
    assert validate_remote_name("upstream") is True
    assert validate_remote_name("--receive-pack=cmd") is False
    assert validate_remote_name("-bad") is False


def test_push_task_branch_disallows_protected_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("git must not run"))
    res = push_task_branch(tmp_path, "main")
    assert res.kind is PushResultKind.DISALLOWED_BRANCH


def test_push_task_branch_disallows_invalid_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("git must not run"))
    res = push_task_branch(tmp_path, "feature/valid", remote="--invalid")
    assert res.kind is PushResultKind.FAILURE


def test_push_task_branch_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="Everything up-to-date", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = push_task_branch(tmp_path, "feature/x")
    assert res.kind is PushResultKind.SUCCESS
    assert calls == [("git", "push", "-u", "--", "origin", "HEAD:refs/heads/feature/x")]


def test_push_task_branch_classifications(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 1. Non fast forward
    def fake_nff(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="error: [rejected] (non-fast-forward)",
        )

    monkeypatch.setattr(subprocess, "run", fake_nff)
    res = push_task_branch(tmp_path, "feature/x")
    assert res.kind is PushResultKind.NON_FAST_FORWARD

    # 2. Permission denied
    def fake_perm(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="remote: Permission denied to repo",
        )

    monkeypatch.setattr(subprocess, "run", fake_perm)
    res = push_task_branch(tmp_path, "feature/x")
    assert res.kind is PushResultKind.PERMISSION_DENIED
