"""Prepare a CI-authenticated cache for one clean verification snapshot."""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.agents.base import ProcessExecutionRequest, ProcessExecutionResult
from subsched.agents.isolation import IsolationGitContext
from subsched.agents.process import run_process_group
from subsched.audit_cache import materialize_audit_cache
from subsched.audit_github import AuditTransportIsolationError, fetch_github_cache
from subsched.audit_inventory import canonical_inventory
from subsched.audit_locked_inventory import AuditIsolationError, _read, derive_locked_inventory
from subsched.audit_provenance import AuditPolicy
from subsched.config import IndependentAuditConfig


class AuditUnavailable(ValueError):
    """The external audit is missing, untrusted, stale or for a different target."""

    def __init__(self, *, cleanup_confirmed: bool = True) -> None:
        super().__init__("independent dependency audit is unavailable or untrusted")
        self.cleanup_confirmed = cleanup_confirmed


def _checked_lock(
    worktree: Path,
    git_context: IsolationGitContext,
    home: Path,
    runner: Callable[[ProcessExecutionRequest], ProcessExecutionResult],
) -> bytes:
    git = shutil.which("git")
    if git is None or worktree.is_symlink():
        raise ValueError("trusted Git or worktree is unavailable")
    cwd = worktree.resolve(strict=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    git_prefix = (
        str(Path(git).resolve()),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
    )
    snapshot_prefix = (*git_prefix, f"--git-dir={git_context.git_dir}", f"--work-tree={cwd}")

    def execute(argv: tuple[str, ...], limit: int = 8 * 1024 * 1024) -> str:
        response = runner(
            ProcessExecutionRequest(
                argv=argv, cwd=cwd, env=env, timeout_seconds=30, output_limit_bytes=limit
            )
        )
        if not response.cleanup_succeeded:
            raise AuditIsolationError("Git process cleanup unconfirmed")
        if (
            response.exit_code != 0
            or response.timed_out
            or response.output_limit_exceeded
            or response.command_not_found
        ):
            raise ValueError("Git snapshot cannot be validated")
        return response.stdout

    if (cwd / ".git").is_symlink():
        raise ValueError("unsafe source Git entry")
    source_head = execute((*git_prefix, "-C", str(cwd), "rev-parse", "--verify", "HEAD"), 4096)
    if source_head.strip() != git_context.base_commit:
        raise ValueError("source Git HEAD changed")
    if execute((*snapshot_prefix, "status", "--porcelain=v1", "--untracked-files=all")):
        raise ValueError("worktree must be clean before auditing")
    lock_bytes = b""
    for name in ("uv.lock", "pyproject.toml", "scripts/offline-build-requirements.txt"):
        data = _read(cwd / name)
        if (
            execute(
                (*snapshot_prefix, "cat-file", "blob", f"{git_context.base_commit}:{name}")
            ).encode()
            != data
        ):
            raise ValueError("worktree does not match its committed audit inputs")
        if name == "uv.lock":
            lock_bytes = data
    return lock_bytes


def build_audit_target(
    worktree: Path,
    git_context: IsolationGitContext,
    config: IndependentAuditConfig,
    task_repository: str,
    image: str,
    runtime: Path,
    home: Path,
    *,
    runner: Callable[[ProcessExecutionRequest], ProcessExecutionResult] = run_process_group,
    derive: Callable[..., list[tuple[str, str]]] = derive_locked_inventory,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Derive a target from a clean commit and a data-only isolated environment."""
    if (
        config.repository != task_repository
        or re.fullmatch(r"[0-9a-f]{40}", git_context.base_commit) is None
    ):
        raise ValueError("audit repository or commit does not match")
    lock = _checked_lock(worktree, git_context, home, runner)
    inventory = derive(worktree.resolve(strict=True), image, runtime)
    if ("pip-audit", config.tool.removeprefix("pip-audit ")) not in inventory:
        raise ValueError("audit tool version does not match the target environment")
    if _checked_lock(worktree, git_context, home, runner) != lock:
        raise ValueError("audit target changed during inventory resolution")
    target = {
        "repository": task_repository,
        "commit": git_context.base_commit,
        "lock_sha256": hashlib.sha256(lock).hexdigest(),
        "packages_sha256": hashlib.sha256(canonical_inventory(inventory)).hexdigest(),
        "tool": config.tool,
        "policy": config.policy,
        "workflow_sha256": config.workflow_sha256,
    }
    return target, inventory


def prepare_independent_audit(
    worktree: Path,
    git_context: IsolationGitContext,
    config: IndependentAuditConfig,
    task_repository: str,
    image: str,
    runtime: Path,
    cache_root: Path,
    *,
    gh: Path | None = None,
    runner: Callable[[ProcessExecutionRequest], ProcessExecutionResult] = run_process_group,
    derive: Callable[..., list[tuple[str, str]]] = derive_locked_inventory,
    fetch: Callable[..., dict[str, bytes] | None] = fetch_github_cache,
    now: datetime | None = None,
) -> Path:
    """Fetch clean exact-target evidence; never import untrusted cache on failure."""
    try:
        gh_executable = gh or (Path(found).resolve() if (found := shutil.which("gh")) else None)
        if gh_executable is None:
            raise ValueError("GitHub CLI is unavailable")
        if config.run_id is None or config.artifact_id is None:
            raise ValueError("independent audit run and artifact are not configured")
        target, inventory = build_audit_target(
            worktree,
            git_context,
            config,
            task_repository,
            image,
            runtime,
            cache_root.parent,
            runner=runner,
            derive=derive,
        )
        policy = AuditPolicy(
            config.repository_id,
            config.workflow_id,
            config.workflow_commit,
            target,
            timedelta(seconds=config.max_age_seconds),
        )
        cache = fetch(
            config.repository,
            config.run_id,
            config.artifact_id,
            policy,
            now or datetime.now(UTC),
            inventory,
            gh=gh_executable,
        )
        if (
            cache is None
            or hashlib.sha256(
                _checked_lock(worktree, git_context, cache_root.parent, runner)
            ).hexdigest()
            != target["lock_sha256"]
        ):
            raise ValueError("audit evidence is unavailable")
        materialize_audit_cache(cache_root, cache, inventory)
        return cache_root
    except (AuditIsolationError, AuditTransportIsolationError) as error:
        raise AuditUnavailable(cleanup_confirmed=False) from error
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        raise AuditUnavailable() from error
