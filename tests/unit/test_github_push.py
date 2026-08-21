from __future__ import annotations

from pathlib import Path

from subsched.github.push import PushResultKind, push_task_branch, validate_branch_name


def test_validate_branch_name() -> None:
    assert validate_branch_name("main") is False
    assert validate_branch_name("master") is False
    assert validate_branch_name("release") is False
    assert validate_branch_name("subsched/issue-103") is True
    assert validate_branch_name("issue/103-fix-bug") is True


def test_push_task_branch_disallows_protected_branch(tmp_path: Path) -> None:
    res = push_task_branch(tmp_path, "main")
    assert res.kind is PushResultKind.DISALLOWED_BRANCH
