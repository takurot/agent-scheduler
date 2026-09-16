"""Opt-in adversarial tests for the real native container boundary."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from subsched.agents.base import ProcessExecutionRequest
from subsched.agents.isolation import (
    cleanup_native_container,
    import_isolated_git,
    native_container_name,
    prepare_isolated_git,
    verify_native_isolation,
    wrap_native_request,
)
from subsched.agents.process import run_process_group
from subsched.config import NativeIsolationConfig


def _live_config(auth: Path) -> tuple[NativeIsolationConfig, Path]:
    if os.environ.get("SUBSCHED_DOCKER_ISOLATION_TEST") != "1":
        pytest.skip("set SUBSCHED_DOCKER_ISOLATION_TEST=1 for the real isolation test")
    runtime_raw = shutil.which("docker")
    if runtime_raw is None:
        pytest.fail("docker is required when the real isolation test is enabled")
    required = {
        name: os.environ.get(name)
        for name in (
            "SUBSCHED_ISOLATION_WORKER_IMAGE",
            "SUBSCHED_ISOLATION_PROXY_IMAGE",
            "SUBSCHED_ISOLATION_NETWORK",
            "SUBSCHED_ISOLATION_PROXY_URL",
        )
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        pytest.fail(f"missing real isolation test settings: {', '.join(missing)}")
    return (
        NativeIsolationConfig(
            backend="container",
            runtime="docker",
            image=required["SUBSCHED_ISOLATION_WORKER_IMAGE"],
            network=required["SUBSCHED_ISOLATION_NETWORK"],
            proxy_url=required["SUBSCHED_ISOLATION_PROXY_URL"],
            proxy_image=required["SUBSCHED_ISOLATION_PROXY_IMAGE"],
            auth=(("codex", auth),),
        ),
        Path(runtime_raw),
    )


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path, NativeIsolationConfig, Path]:
    auth = tmp_path / "provider-auth"
    auth.mkdir(mode=0o700)
    (auth / "auth.json").write_text("synthetic-provider-auth\n", encoding="utf-8")
    (auth / "auth.json").chmod(0o600)
    config, runtime = _live_config(auth)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git(worktree, "init", "--quiet", "-b", "main")
    tracked = worktree / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    _git(worktree, "add", "tracked.txt")
    _git(
        worktree,
        "-c",
        "user.name=Host",
        "-c",
        "user.email=host@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "initial",
    )
    return worktree, auth, config, runtime


def test_real_container_blocks_host_credentials_and_imports_task_commit(
    tmp_path: Path,
) -> None:
    worktree, _auth, config, runtime = _fixture(tmp_path)
    host_canary = tmp_path / "synthetic-host-gh-token"
    host_canary.write_text("scheduler_write_credential_canary\n", encoding="utf-8")
    host_canary.chmod(0o600)
    assert verify_native_isolation(
        config, enabled_agents=("codex",), resolver=lambda _: runtime
    ) is None
    git_context = prepare_isolated_git(
        worktree, tmp_path / "isolation-state", "github-293"
    )
    name = native_container_name("github-293")
    script = """
test "$HOME" = /isolated-home
test -f "$HOME/auth.json"
test ! -e "$1"
test ! -e /var/run/docker.sock
test "$GIT_DIR" = /run/subsched-git
test -z "$(git config --get credential.helper || true)"
curl --silent --show-error --connect-timeout 10 https://api.openai.com/v1/models >/dev/null
if curl --noproxy '*' --silent --show-error --connect-timeout 3 \
  https://example.com >/dev/null 2>&1; then
  exit 41
fi
printf 'after\\n' > tracked.txt
git add tracked.txt
git -c user.name=Worker -c user.email=worker@example.invalid commit --quiet -m 'isolated commit'
printf 'ok\\n'
"""
    request = ProcessExecutionRequest(
        argv=("/bin/sh", "-ceu", script, "isolation-test", str(host_canary)),
        cwd=worktree,
        env={"HOME": os.environ["HOME"], "PATH": os.environ["PATH"]},
        timeout_seconds=30,
    )
    wrapped = wrap_native_request(
        request,
        agent="codex",
        config=config,
        runtime_executable=runtime,
        git_dir=git_context.git_dir,
        worktree_git_mount=git_context.worktree_git_mount,
        container_name=name,
    )

    result = run_process_group(wrapped)
    cleanup_failure = cleanup_native_container(runtime, name, env=wrapped.env)

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert cleanup_failure is None
    assert import_isolated_git(worktree, git_context, read_only=False) is None
    assert _git(worktree, "log", "-1", "--format=%s").stdout.strip() == "isolated commit"
    assert host_canary.read_text(encoding="utf-8") == "scheduler_write_credential_canary\n"


def test_real_container_timeout_cleanup_kills_descendants(tmp_path: Path) -> None:
    worktree, _auth, config, runtime = _fixture(tmp_path)
    git_context = prepare_isolated_git(
        worktree, tmp_path / "isolation-state", "github-293-timeout"
    )
    name = native_container_name("github-293-timeout")
    request = ProcessExecutionRequest(
        argv=("/bin/sh", "-c", "sleep 300 & wait"),
        cwd=worktree,
        env={"HOME": os.environ["HOME"], "PATH": os.environ["PATH"]},
        timeout_seconds=0.5,
        grace_seconds=0.5,
    )
    wrapped = wrap_native_request(
        request,
        agent="codex",
        config=config,
        runtime_executable=runtime,
        git_dir=git_context.git_dir,
        worktree_git_mount=git_context.worktree_git_mount,
        container_name=name,
    )

    result = run_process_group(wrapped)
    cleanup_failure = cleanup_native_container(runtime, name, env=wrapped.env)

    assert result.timed_out is True
    assert cleanup_failure is None


def test_real_container_cannot_rewrite_linked_worktree_git_pointer(
    tmp_path: Path,
) -> None:
    _regular, auth, config, runtime = _fixture(tmp_path)
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet", "-b", "main")
    (repository / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(
        repository,
        "-c",
        "user.name=Host",
        "-c",
        "user.email=host@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "initial",
    )
    worktree = tmp_path / "linked-worktree"
    _git(repository, "worktree", "add", "--quiet", "-b", "issue-293", str(worktree))
    original_pointer = (worktree / ".git").read_bytes()
    git_context = prepare_isolated_git(
        worktree, tmp_path / "linked-state", "github-293-linked"
    )
    assert git_context.worktree_git_mount.is_file()
    name = native_container_name("github-293-linked")
    request = ProcessExecutionRequest(
        argv=(
            "/bin/sh",
            "-ceu",
            "if printf 'evil\\n' > .git; then exit 42; fi; git status --short >/dev/null",
        ),
        cwd=worktree,
        env={"HOME": os.environ["HOME"], "PATH": os.environ["PATH"]},
        timeout_seconds=30,
    )
    wrapped = wrap_native_request(
        request,
        agent="codex",
        config=NativeIsolationConfig(
            backend=config.backend,
            runtime=config.runtime,
            image=config.image,
            network=config.network,
            proxy_url=config.proxy_url,
            proxy_image=config.proxy_image,
            auth=(("codex", auth),),
        ),
        runtime_executable=runtime,
        git_dir=git_context.git_dir,
        worktree_git_mount=git_context.worktree_git_mount,
        container_name=name,
    )

    result = run_process_group(wrapped)
    cleanup_failure = cleanup_native_container(runtime, name, env=wrapped.env)

    assert result.exit_code == 0, result.stderr
    assert cleanup_failure is None
    assert (worktree / ".git").read_bytes() == original_pointer


def _build_local_bwrap_image(tag: str) -> None:
    """Build a small local image with Bubblewrap installed, so this test can reproduce
    the exact kernel-level conflict (issue #314) without depending on the
    operator-provisioned worker/proxy images the other real-container tests require.

    `NativeIsolationConfig` built directly in Python (as here) skips the YAML loader's
    digest-pinning format check, so a locally built, uniquely tagged image reference is
    sufficient -- `wrap_native_request` does not itself require a `@sha256:` reference.
    """
    subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "-q", "-t", tag, "-"],
        input="FROM alpine:3.20\nRUN apk add --no-cache bubblewrap\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )


def test_real_container_reproduces_and_resolves_codex_bwrap_sandbox_conflict(
    tmp_path: Path,
) -> None:
    """#314: Codex's own `workspace-write`/`read-only` sandbox shells out to Bubblewrap
    (`bwrap`) to create an unprivileged user namespace. Inside the Scheduler's container
    isolation boundary (`--cap-drop ALL` + `--security-opt no-new-privileges=true`), the
    kernel refuses that namespace creation and every command fails -- reproduced here
    with the exact error text from the issue. Telling Codex to trust the outer sandbox
    instead (no bwrap invocation, i.e. `danger-full-access` -- see
    `resolve_codex_sandbox_mode`) must let the same container run ordinary commands.
    """
    if os.environ.get("SUBSCHED_DOCKER_ISOLATION_TEST") != "1":
        pytest.skip("set SUBSCHED_DOCKER_ISOLATION_TEST=1 for the real isolation test")
    runtime_raw = shutil.which("docker")
    if runtime_raw is None:
        pytest.fail("docker is required when the real isolation test is enabled")
    runtime = Path(runtime_raw)
    auth = tmp_path / "auth"
    auth.mkdir(mode=0o700)
    (auth / "auth.json").write_text("synthetic-auth\n", encoding="utf-8")
    (auth / "auth.json").chmod(0o600)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    network = f"subsched-bwrap-repro-{os.getpid()}"
    image = f"subsched-bwrap-repro:{os.getpid()}"
    subprocess.run(["docker", "network", "create", network], check=True, timeout=30)
    try:
        _build_local_bwrap_image(image)
        config = NativeIsolationConfig(
            backend="container",
            runtime="docker",
            image=image,
            network=network,
            proxy_url="http://127.0.0.1:1",
            proxy_image=image,
            auth=(("codex", auth),),
        )

        def run_script(label: str, script: str) -> subprocess.CompletedProcess[str]:
            name = native_container_name(f"bwrapreprotest{label}"[:80])
            request = ProcessExecutionRequest(
                argv=("/bin/sh", "-ceu", script),
                cwd=worktree,
                env={"HOME": os.environ["HOME"], "PATH": os.environ["PATH"]},
                timeout_seconds=30,
            )
            wrapped = wrap_native_request(
                request,
                agent="codex",
                config=config,
                runtime_executable=runtime,
                container_name=name,
            )
            try:
                return run_process_group(wrapped)
            finally:
                cleanup_native_container(runtime, name, env=wrapped.env)

        # The bug: Codex's own sandbox invokes bwrap, which the kernel refuses inside
        # the outer container boundary -- exactly the field-reported failure.
        bwrap_result = run_script(
            "old",
            "bwrap --unshare-user --bind / / -- /bin/sh -c 'echo inner-ok'",
        )
        assert bwrap_result.exit_code != 0
        assert "No permissions to creat" in bwrap_result.stderr
        assert "namespace" in bwrap_result.stderr

        # The fix: without bwrap in the path (danger-full-access trusts the outer
        # sandbox), the identical container boundary runs a plain command normally.
        direct_result = run_script("new", "echo direct-ok")
        assert direct_result.exit_code == 0
        assert direct_result.stdout.strip() == "direct-ok"
    finally:
        subprocess.run(["docker", "network", "rm", network], check=False, timeout=30)
        subprocess.run(["docker", "image", "rm", image], check=False, timeout=30)
