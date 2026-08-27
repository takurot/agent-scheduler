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
