from __future__ import annotations

from pathlib import Path

import pytest

from subsched.agents.base import ProcessExecutionRequest
from subsched.agents.isolation import IsolationGitContext, wrap_verification_request
from subsched.config import NativeIsolationConfig


def test_verified_cache_is_read_only_and_isolated(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    context = IsolationGitContext(git_dir, "a" * 40, git_dir)
    request = ProcessExecutionRequest(argv=("true",), cwd=worktree, env={})
    config = NativeIsolationConfig(backend="container", image="image@sha256:" + "a" * 64)
    wrapped = wrap_verification_request(
        request,
        config=config,
        runtime_executable=Path("/usr/bin/true"),
        container_name="subsched-test",
        git_context=context,
        audit_cache=cache,
    )
    assert f"type=bind,src={cache},dst=/run/pip-audit,readonly" in wrapped.argv
    assert "XDG_CACHE_HOME=/run" in wrapped.argv
    assert wrapped.argv[wrapped.argv.index("--network") + 1] == "none"
    assert not any("auth" in argument.lower() for argument in wrapped.argv)


def test_rejects_symlinked_cache(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    cache = tmp_path / "cache"
    cache.symlink_to(tmp_path)
    context = IsolationGitContext(git_dir, "a" * 40, git_dir)
    request = ProcessExecutionRequest(argv=("true",), cwd=worktree, env={})
    config = NativeIsolationConfig(backend="container", image="image@sha256:" + "a" * 64)
    with pytest.raises(ValueError):
        wrap_verification_request(
            request,
            config=config,
            runtime_executable=Path("/usr/bin/true"),
            container_name="subsched-test",
            git_context=context,
            audit_cache=cache,
        )
