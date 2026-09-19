from __future__ import annotations

from pathlib import Path

import pytest

from subsched.gitenv import GIT_LOCATION_OVERRIDE_VARS, git_safe_env


def test_git_safe_env_strips_location_override_vars() -> None:
    base = {
        "PATH": "/usr/bin",
        "GIT_DIR": "/somewhere/.git",
        "GIT_WORK_TREE": "/somewhere",
        "GIT_INDEX_FILE": "/somewhere/.git/index",
        "GIT_COMMON_DIR": "/somewhere/.git",
        "GIT_CEILING_DIRECTORIES": "/somewhere",
        "GIT_OBJECT_DIRECTORY": "/somewhere/.git/objects",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/somewhere/.git/objects",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/somewhere/.gitconfig",
        "GIT_CONFIG_SYSTEM": "/somewhere/gitconfig",
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    result = git_safe_env(base)

    assert result == {"PATH": "/usr/bin"}
    for name in GIT_LOCATION_OVERRIDE_VARS:
        assert name not in result


def test_git_safe_env_preserves_unrelated_vars() -> None:
    base = {"PATH": "/usr/bin", "HOME": "/home/user", "LANG": "C.UTF-8"}

    result = git_safe_env(base)

    assert result == base
    assert result is not base


def test_git_safe_env_defaults_to_current_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/leaked/.git")
    monkeypatch.setenv("SUBSCHED_TEST_MARKER", "1")

    result = git_safe_env()

    assert "GIT_DIR" not in result
    assert result.get("SUBSCHED_TEST_MARKER") == "1"


def test_ensure_git_exclude_creates_and_populates_exclude_file(tmp_path: Path) -> None:
    from subsched.gitenv import ensure_git_exclude

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    exclude_path = ensure_git_exclude(repo_dir, ".ai/")

    assert exclude_path.exists()
    assert ".ai/" in exclude_path.read_text(encoding="utf-8")


def test_ensure_git_exclude_is_idempotent(tmp_path: Path) -> None:
    from subsched.gitenv import ensure_git_exclude

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    ensure_git_exclude(repo_dir, ".ai/")
    content_first = (repo_dir / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    ensure_git_exclude(repo_dir, ".ai/")
    content_second = (repo_dir / ".git" / "info" / "exclude").read_text(encoding="utf-8")

    assert content_first == content_second
    assert content_second.count(".ai/") == 1
