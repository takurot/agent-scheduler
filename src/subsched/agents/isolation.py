"""Mechanically verified isolation for native subscription-backed workers."""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from subsched.agents.base import ProcessExecutionRequest
from subsched.agents.process import COMMON_ENV_ALLOWLIST, filter_environment
from subsched.config import NativeIsolationConfig, validate_base_branch
from subsched.gitenv import ensure_git_exclude, git_safe_env
from subsched.storage import atomic_write_secure_bytes, secure_directory

_MAX_AUTH_BYTES = 1_048_576
_TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


@dataclass(frozen=True, slots=True)
class IsolationGitContext:
    """Task-specific Git data that is safe to expose to one worker invocation."""

    git_dir: Path
    base_commit: str
    worktree_git_mount: Path


def _auth_failure(path: Path) -> str | None:
    """Validate credential input without reading or exposing credential contents."""
    if path.is_symlink():
        return "native isolation auth path must not be a symlink"
    if not path.is_dir():
        return "native isolation auth path must be an existing directory"
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            return "native isolation auth directory permissions are too broad"
        total_bytes = 0
        for child in path.rglob("*"):
            if child.is_symlink():
                return "native isolation auth contents must not contain symlinks"
            child_stat = child.stat()
            if child.is_dir():
                if stat.S_IMODE(child_stat.st_mode) & 0o077:
                    return "native isolation auth directory permissions are too broad"
                continue
            if not child.is_file():
                return "native isolation auth contents must contain only regular files"
            if stat.S_IMODE(child_stat.st_mode) & 0o077:
                return "native isolation auth file permissions are too broad"
            total_bytes += child_stat.st_size
            if total_bytes > _MAX_AUTH_BYTES:
                return "native isolation auth contents exceed the size limit"
    except OSError:
        return "native isolation auth path could not be securely inspected"
    return None


def _json_output(
    run_cmd: Callable[..., subprocess.CompletedProcess[str]], argv: list[str]
) -> tuple[Any | None, str | None]:
    try:
        result = run_cmd(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env=filter_environment(dict(os.environ), allowlist=COMMON_ENV_ALLOWLIST),
        )
    except (OSError, subprocess.SubprocessError):
        return None, "native isolation runtime inspection failed"
    if result.returncode != 0:
        return None, "native isolation runtime inspection failed"
    try:
        return json.loads(result.stdout), None
    except (json.JSONDecodeError, TypeError):
        return None, "native isolation runtime returned an invalid inspection result"


def _git(
    argv: list[str], *, run_cmd: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> subprocess.CompletedProcess[str]:
    return run_cmd(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=git_safe_env(),
    )


def prepare_isolated_git(
    worktree: Path,
    state_root: Path,
    task_id: str,
    *,
    base_branch: str = "main",
    run_cmd: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> IsolationGitContext:
    """Create a private Git database seeded from the task HEAD and its review base."""
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("native isolation task id is unsafe")
    validated_base = validate_base_branch(base_branch)
    if not state_root.is_absolute() or state_root.is_symlink():
        raise ValueError("native isolation state root must be an absolute non-symlink path")
    resolved_worktree = worktree.resolve(strict=True)
    resolved_state = state_root.resolve()
    if resolved_state == resolved_worktree or resolved_worktree in resolved_state.parents:
        raise ValueError("native isolation Git state must be outside the task worktree")
    secure_directory(state_root)
    task_root = state_root / task_id
    secure_directory(task_root)
    invocation_root = task_root / f"invocation-{secrets.token_hex(8)}"
    secure_directory(invocation_root)
    git_dir = invocation_root / "repo.git"

    head = _git(["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"], run_cmd=run_cmd)
    base_commit = head.stdout.strip()
    if head.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", base_commit):
        raise ValueError("native isolation could not resolve the task Git HEAD")
    remote_base_ref = f"refs/remotes/origin/{validated_base}"
    local_base_ref = f"refs/heads/{validated_base}"
    if (
        _git(
            ["git", "-C", str(worktree), "show-ref", "--verify", "--quiet", remote_base_ref],
            run_cmd=run_cmd,
        ).returncode
        == 0
    ):
        base_source_ref = remote_base_ref
    elif (
        _git(
            ["git", "-C", str(worktree), "show-ref", "--verify", "--quiet", local_base_ref],
            run_cmd=run_cmd,
        ).returncode
        == 0
    ):
        base_source_ref = local_base_ref
    else:
        raise ValueError("native isolation could not resolve the configured base branch")
    commands = (
        ["git", "init", "--quiet", "--bare", str(git_dir)],
        [
            "git",
            f"--git-dir={git_dir}",
            "fetch",
            "--quiet",
            "--no-tags",
            str(worktree),
            f"HEAD:refs/heads/{task_id}",
        ],
        [
            "git",
            f"--git-dir={git_dir}",
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            str(worktree),
            f"{base_source_ref}:{remote_base_ref}",
        ],
        [
            "git",
            f"--git-dir={git_dir}",
            "rev-parse",
            "--quiet",
            "--verify",
            f"{remote_base_ref}^{{commit}}",
        ],
        [
            "git",
            f"--git-dir={git_dir}",
            "symbolic-ref",
            "HEAD",
            f"refs/heads/{task_id}",
        ],
        [
            "git",
            f"--git-dir={git_dir}",
            f"--work-tree={worktree}",
            "reset",
            "--quiet",
            "--mixed",
            "HEAD",
        ],
        [
            "git",
            f"--git-dir={git_dir}",
            "config",
            "core.hooksPath",
            "/dev/null",
        ],
        [
            "git",
            f"--git-dir={git_dir}",
            "config",
            "core.bare",
            "false",
        ],
    )
    for command in commands:
        if _git(command, run_cmd=run_cmd).returncode != 0:
            raise ValueError("native isolation could not prepare task-specific Git metadata")
    ensure_git_exclude(git_dir, run_cmd=run_cmd)
    worktree_git = worktree / ".git"
    if worktree_git.is_symlink():
        raise ValueError("native isolation refuses a symlinked worktree .git entry")
    if worktree_git.is_file():
        worktree_git_mount = invocation_root / "worktree.git"
        atomic_write_secure_bytes(worktree_git_mount, b"gitdir: /run/subsched-git\n")
    elif worktree_git.is_dir():
        worktree_git_mount = git_dir
    else:
        raise ValueError("native isolation worktree .git entry is invalid")
    return IsolationGitContext(
        git_dir=git_dir,
        base_commit=base_commit,
        worktree_git_mount=worktree_git_mount,
    )


def import_isolated_git(
    worktree: Path,
    context: IsolationGitContext,
    *,
    read_only: bool,
    run_cmd: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Import only a valid descendant HEAD from a task-specific Git database."""
    head = _git(
        ["git", f"--git-dir={context.git_dir}", "rev-parse", "--verify", "HEAD"],
        run_cmd=run_cmd,
    )
    sandbox_commit = head.stdout.strip()
    if head.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", sandbox_commit):
        return "native isolation Git result is invalid"
    if read_only and sandbox_commit != context.base_commit:
        return "native isolation read-only stage created a commit"
    if sandbox_commit == context.base_commit:
        return None
    ancestor = _git(
        [
            "git",
            f"--git-dir={context.git_dir}",
            "merge-base",
            "--is-ancestor",
            context.base_commit,
            sandbox_commit,
        ],
        run_cmd=run_cmd,
    )
    if ancestor.returncode != 0:
        return "native isolation Git result is not descended from the task HEAD"
    fsck = _git(["git", f"--git-dir={context.git_dir}", "fsck", "--no-dangling"], run_cmd=run_cmd)
    if fsck.returncode != 0:
        return "native isolation Git result failed object validation"
    host_head = _git(
        ["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
        run_cmd=run_cmd,
    )
    if host_head.returncode != 0 or host_head.stdout.strip() != context.base_commit:
        return "native isolation task branch changed before importing the commit"
    fetched = _git(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "protocol.file.allow=always",
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            str(context.git_dir),
            sandbox_commit,
        ],
        run_cmd=run_cmd,
    )
    if fetched.returncode != 0:
        return "native isolation could not import the task commit"
    index = _git(
        ["git", "-C", str(worktree), "read-tree", sandbox_commit],
        run_cmd=run_cmd,
    )
    if index.returncode != 0:
        return "native isolation could not stage the task Git index"
    updated = _git(
        [
            "git",
            "-C",
            str(worktree),
            "update-ref",
            "HEAD",
            sandbox_commit,
            context.base_commit,
        ],
        run_cmd=run_cmd,
    )
    if updated.returncode != 0:
        _git(
            ["git", "-C", str(worktree), "reset", "--quiet", "--mixed", context.base_commit],
            run_cmd=run_cmd,
        )
        return "native isolation task branch changed while importing the commit"
    return None


def probe_container_toolchain(
    runtime: str | Path,
    image: str,
    binary: str,
    *,
    run_cmd: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = 15.0,
) -> bool:
    """Probe whether `binary` exists in the container image's PATH (#364)."""
    try:
        result = run_cmd(
            [
                str(runtime),
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                image,
                "-c",
                f"command -v {shlex.quote(binary)}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=filter_environment(dict(os.environ), allowlist=COMMON_ENV_ALLOWLIST),
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def verify_native_isolation(
    config: NativeIsolationConfig,
    *,
    enabled_agents: Sequence[str],
    resolver: Callable[[str], Path | str | None] = shutil.which,
    run_cmd: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Attest the runtime, pinned image, internal network, and credential inputs."""
    if config.backend != "container":
        return (
            "native isolation unverified: no mechanically verified container backend; "
            "worker credential tier: unverified (dispatch blocked)"
        )
    runtime_raw = resolver(config.runtime)
    if runtime_raw is None:
        return "native isolation runtime is unavailable"
    runtime = Path(runtime_raw)
    if not runtime.is_absolute():
        return "native isolation runtime path is not absolute"
    if (
        config.image is None
        or config.network is None
        or config.proxy_url is None
        or config.proxy_image is None
    ):
        return "native isolation container configuration is incomplete"

    auth_by_agent = dict(config.auth)
    for agent in enabled_agents:
        auth = auth_by_agent.get(agent)
        if auth is None:
            return f"native isolation auth is not configured for {agent}"
        failure = _auth_failure(auth)
        if failure is not None:
            return failure

    os_type, failure = _json_output(run_cmd, [str(runtime), "info", "--format", "{{json .OSType}}"])
    if failure is not None:
        return failure
    if os_type != "linux":
        return "native isolation runtime must execute Linux containers"

    worker_image, failure = _json_output(
        run_cmd,
        [
            str(runtime),
            "image",
            "inspect",
            config.image,
            "--format",
            "{{json .}}",
        ],
    )
    if failure is not None:
        return failure
    worker_digests = worker_image.get("RepoDigests") if isinstance(worker_image, dict) else None
    worker_config = worker_image.get("Config") if isinstance(worker_image, dict) else None
    if (
        not isinstance(worker_digests, list)
        or any(not isinstance(value, str) for value in worker_digests)
        or config.image not in worker_digests
        or not isinstance(worker_config, dict)
        or worker_config.get("Volumes") not in (None, {})
    ):
        return "native isolation image digest does not match the configured digest"

    network, failure = _json_output(
        run_cmd,
        [str(runtime), "network", "inspect", config.network, "--format", "{{json .}}"],
    )
    if failure is not None:
        return failure
    if not isinstance(network, dict) or network.get("Internal") is not True:
        return "native isolation network must be internal"
    proxy_name = urlsplit(config.proxy_url).hostname
    peers = network.get("Containers")
    if proxy_name is None or not isinstance(peers, dict):
        return "native isolation proxy attachment could not be verified"
    if any(
        not isinstance(peer, dict) or not isinstance(peer.get("Name"), str)
        for peer in peers.values()
    ):
        return "native isolation proxy attachment returned an invalid result"
    peer_names = {peer["Name"] for peer in peers.values()}
    if peer_names != {proxy_name}:
        return "native isolation network must contain only the configured proxy"

    proxy_image, failure = _json_output(
        run_cmd,
        [
            str(runtime),
            "image",
            "inspect",
            config.proxy_image,
            "--format",
            "{{json .}}",
        ],
    )
    if failure is not None:
        return failure
    proxy_digests = proxy_image.get("RepoDigests") if isinstance(proxy_image, dict) else None
    proxy_image_id = proxy_image.get("Id") if isinstance(proxy_image, dict) else None
    proxy_image_config = proxy_image.get("Config") if isinstance(proxy_image, dict) else None
    if (
        not isinstance(proxy_digests, list)
        or any(not isinstance(value, str) for value in proxy_digests)
        or config.proxy_image not in proxy_digests
        or not isinstance(proxy_image_id, str)
        or not isinstance(proxy_image_config, dict)
    ):
        return "native isolation proxy image digest does not match"
    proxy, failure = _json_output(
        run_cmd,
        [str(runtime), "container", "inspect", proxy_name, "--format", "{{json .}}"],
    )
    if failure is not None:
        return failure
    proxy_state = proxy.get("State") if isinstance(proxy, dict) else None
    proxy_config = proxy.get("Config") if isinstance(proxy, dict) else None
    host_config = proxy.get("HostConfig") if isinstance(proxy, dict) else None
    mounts = proxy.get("Mounts") if isinstance(proxy, dict) else None
    network_settings = proxy.get("NetworkSettings") if isinstance(proxy, dict) else None
    proxy_networks = (
        network_settings.get("Networks") if isinstance(network_settings, dict) else None
    )
    security_options = host_config.get("SecurityOpt") if isinstance(host_config, dict) else None
    if (
        not isinstance(proxy, dict)
        or not isinstance(proxy_state, dict)
        or proxy_state.get("Running") is not True
        or proxy.get("Image") != proxy_image_id
        or not isinstance(proxy_config, dict)
        or any(
            proxy_config.get(key) != proxy_image_config.get(key)
            for key in ("Entrypoint", "Cmd", "Env", "User")
        )
        or proxy_config.get("User") in (None, "", "0", "root")
        or not isinstance(host_config, dict)
        or host_config.get("Privileged") is not False
        or host_config.get("ReadonlyRootfs") is not True
        or host_config.get("CapAdd") not in (None, [])
        or host_config.get("CapDrop") != ["ALL"]
        or host_config.get("Binds") not in (None, [])
        or host_config.get("Devices") != []
        or host_config.get("PidMode") != ""
        or host_config.get("IpcMode") != "private"
        or host_config.get("NetworkMode") != "bridge"
        or not isinstance(security_options, list)
        or any(not isinstance(value, str) for value in security_options)
        or "no-new-privileges=true" not in security_options
        or mounts != []
        or not isinstance(proxy_networks, dict)
        or set(proxy_networks) != {config.network, "bridge"}
    ):
        return "native isolation proxy runtime or network attachment is unverified"
    return None


def _format_cpus(cpus: float) -> str:
    return str(int(cpus)) if cpus.is_integer() else str(cpus)


def _mount_value(source: Path, destination: str, *, readonly: bool = False) -> str:
    if any(character in str(source) for character in (",", "\n", "\r")):
        raise ValueError("native isolation mount path contains an unsafe character")
    suffix = ",readonly" if readonly else ""
    return f"type=bind,src={source},dst={destination}{suffix}"


def native_container_name(task_id: str) -> str:
    """Return an unguessable, runtime-safe name used for verified cleanup."""
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("native isolation task id is unsafe")
    return f"subsched-worker-{task_id[:80]}-{secrets.token_hex(8)}"


def _listed_containers(
    runtime_executable: Path,
    container_name: str,
    env: dict[str, str],
    run_cmd: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[set[str] | None, str | None]:
    try:
        result = run_cmd(
            [
                str(runtime_executable),
                "container",
                "ls",
                "--all",
                "--filter",
                f"name=^/{container_name}$",
                "--format",
                "{{json .Names}}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None, "native isolation container cleanup could not be verified"
    if result.returncode != 0:
        return None, "native isolation container cleanup could not be verified"
    names: set[str] = set()
    try:
        for line in result.stdout.splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, str):
                    raise TypeError
                names.add(value)
    except (json.JSONDecodeError, TypeError):
        return None, "native isolation container cleanup returned an invalid result"
    return names, None


def cleanup_native_container(
    runtime_executable: Path,
    container_name: str,
    *,
    env: dict[str, str],
    run_cmd: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Ensure the invocation container and all descendants are gone."""
    names, failure = _listed_containers(runtime_executable, container_name, env, run_cmd)
    if failure is not None or names is None:
        return failure
    if container_name not in names:
        return None
    try:
        removed = run_cmd(
            [str(runtime_executable), "container", "rm", "--force", container_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return "native isolation container cleanup failed"
    if removed.returncode != 0:
        return "native isolation container cleanup failed"
    remaining, failure = _listed_containers(runtime_executable, container_name, env, run_cmd)
    if failure is not None or remaining is None or container_name in remaining:
        return failure or "native isolation container cleanup could not be confirmed"
    return None


def wrap_native_request(
    request: ProcessExecutionRequest,
    *,
    agent: str,
    config: NativeIsolationConfig,
    runtime_executable: Path,
    git_dir: Path | None = None,
    worktree_git_mount: Path | None = None,
    read_only: bool = False,
    container_name: str | None = None,
    review_reports_dir: Path | None = None,
) -> ProcessExecutionRequest:
    """Wrap a provider CLI request in the previously attested container boundary.

    #324: `review_reports_dir` (when given, must be exactly `<request.cwd>/.ai/reviews`
    and already exist on the host) is bind-mounted writable at that path, overlaying the
    otherwise readonly worktree mount, so a PR_REVIEW dispatch can still write its report
    file without granting write access to the rest of the worktree.
    """
    if config.backend != "container" or config.image is None or config.network is None:
        raise ValueError("native isolation container configuration is incomplete")
    if config.proxy_url is None or not runtime_executable.is_absolute():
        raise ValueError("native isolation runtime configuration is incomplete")
    auth = dict(config.auth).get(agent)
    if auth is None:
        raise ValueError(f"native isolation auth is not configured for {agent}")
    failure = _auth_failure(auth)
    if failure is not None:
        raise ValueError(failure)
    try:
        auth.relative_to(request.cwd)
    except ValueError:
        pass
    else:
        raise ValueError("native isolation auth must be outside the task worktree")
    if agent not in {"claude", "codex"}:
        raise ValueError("native isolation agent is unsupported")
    if git_dir is not None:
        if not git_dir.is_absolute() or git_dir.is_symlink() or not git_dir.is_dir():
            raise ValueError("native isolation Git metadata is invalid")
        try:
            git_dir.relative_to(request.cwd)
        except ValueError:
            pass
        else:
            raise ValueError("native isolation Git metadata must be outside the task worktree")
        if (
            worktree_git_mount is None
            or not worktree_git_mount.is_absolute()
            or worktree_git_mount.is_symlink()
            or not worktree_git_mount.exists()
        ):
            raise ValueError("native isolation worktree Git mount is invalid")

    if review_reports_dir is not None:
        expected_reviews_dir = request.cwd / ".ai" / "reviews"
        if (
            not review_reports_dir.is_absolute()
            or review_reports_dir.is_symlink()
            or not review_reports_dir.is_dir()
            or review_reports_dir != expected_reviews_dir
        ):
            raise ValueError("native isolation review reports directory is invalid")

    uid = os.getuid()
    gid = os.getgid()
    home_variable = "CODEX_HOME" if agent == "codex" else "CLAUDE_CONFIG_DIR"
    argv = (
        str(runtime_executable),
        "run",
        "--rm",
        "--init",
        "--interactive",
        *(("--name", container_name) if container_name is not None else ()),
        "--user",
        f"{uid}:{gid}",
        "--network",
        config.network,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--pids-limit",
        str(config.pids_limit),
        "--memory",
        config.memory,
        "--cpus",
        _format_cpus(config.cpus),
        "--read-only",
        "--tmpfs",
        f"/tmp:rw,exec,nosuid,nodev,size={config.tmpfs_size},mode=1777,uid={uid},gid={gid}",
        "--tmpfs",
        f"/isolated-home:rw,exec,nosuid,nodev,size=256m,mode=700,uid={uid},gid={gid}",
        "--mount",
        _mount_value(auth, "/run/subsched-auth", readonly=True),
        "--mount",
        _mount_value(request.cwd, str(request.cwd), readonly=read_only),
        *(
            (
                "--mount",
                _mount_value(review_reports_dir, str(request.cwd / ".ai" / "reviews")),
            )
            if review_reports_dir is not None
            else ()
        ),
        "--workdir",
        str(request.cwd),
        "--env",
        "HOME=/isolated-home",
        "--env",
        f"{home_variable}=/isolated-home",
        "--env",
        f"HTTP_PROXY={config.proxy_url}",
        "--env",
        f"HTTPS_PROXY={config.proxy_url}",
        "--env",
        "NO_PROXY=localhost,127.0.0.1",
        *(("--mount", _mount_value(git_dir, "/run/subsched-git")) if git_dir else ()),
        *(
            (
                "--mount",
                _mount_value(
                    worktree_git_mount,
                    str(request.cwd / ".git"),
                    readonly=worktree_git_mount.is_file(),
                ),
            )
            if worktree_git_mount is not None
            else ()
        ),
        "--entrypoint",
        "/bin/sh",
        config.image,
        "-ceu",
        (
            'cp -R /run/subsched-auth/. /isolated-home/ && '
            'if [ -f /isolated-home/oauth-token ]; then '
            'export CLAUDE_CODE_OAUTH_TOKEN="$(cat /isolated-home/oauth-token)"; '
            'fi && exec "$@"'
        ),
        "subsched-entrypoint",
        *request.argv,
    )
    runtime_env = {
        key: value
        for key, value in request.env.items()
        if key in {"HOME", "PATH", "DOCKER_HOST", "CONTAINER_HOST"}
    }
    return ProcessExecutionRequest(
        argv=argv,
        cwd=request.cwd,
        env=runtime_env,
        stdin_payload=request.stdin_payload,
        timeout_seconds=request.timeout_seconds,
        grace_seconds=request.grace_seconds,
        output_limit_bytes=request.output_limit_bytes,
        heartbeat=request.heartbeat,
        heartbeat_interval_seconds=request.heartbeat_interval_seconds,
        plan_review=request.plan_review,
    )


def wrap_verification_request(
    request: ProcessExecutionRequest,
    *,
    config: NativeIsolationConfig,
    runtime_executable: Path,
    container_name: str,
) -> ProcessExecutionRequest:
    """Run a verification gate in the worker image without credentials or network."""
    if config.backend != "container" or config.image is None:
        raise ValueError("verification container configuration is incomplete")
    if not runtime_executable.is_absolute():
        raise ValueError("verification container runtime path is not absolute")
    if not request.cwd.is_absolute() or request.cwd.is_symlink() or not request.cwd.is_dir():
        raise ValueError("verification worktree is not a valid absolute directory")

    uid = os.getuid()
    gid = os.getgid()
    argv = (
        str(runtime_executable),
        "run",
        "--rm",
        "--init",
        "--name",
        container_name,
        "--user",
        f"{uid}:{gid}",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--pids-limit",
        str(config.pids_limit),
        "--memory",
        config.memory,
        "--cpus",
        _format_cpus(config.cpus),
        "--read-only",
        "--tmpfs",
        f"/tmp:rw,exec,nosuid,nodev,size={config.tmpfs_size},mode=1777,uid={uid},gid={gid}",
        "--tmpfs",
        f"/isolated-home:rw,exec,nosuid,nodev,size=256m,mode=700,uid={uid},gid={gid}",
        "--mount",
        _mount_value(request.cwd, str(request.cwd)),
        "--workdir",
        str(request.cwd),
        "--env",
        "HOME=/isolated-home",
        "--entrypoint",
        "/bin/sh",
        config.image,
        "-ceu",
        'exec "$@"',
        "subsched-verification",
        *request.argv,
    )
    runtime_env = {
        key: value
        for key, value in request.env.items()
        if key
        in {
            "PATH",
            "DOCKER_HOST",
            "DOCKER_CERT_PATH",
            "DOCKER_TLS_VERIFY",
            "CONTAINER_HOST",
        }
    }
    return ProcessExecutionRequest(
        argv=argv,
        cwd=request.cwd,
        env=runtime_env,
        timeout_seconds=request.timeout_seconds,
        grace_seconds=request.grace_seconds,
        output_limit_bytes=request.output_limit_bytes,
    )


def native_isolation_failure() -> str | None:
    """Retain fail-closed admission until callers supply and attest a backend."""
    return verify_native_isolation(NativeIsolationConfig(), enabled_agents=())
