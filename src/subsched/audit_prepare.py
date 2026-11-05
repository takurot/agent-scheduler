"""Prepare a CI-authenticated cache for one clean verification snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.agents.base import ProcessExecutionRequest, ProcessExecutionResult
from subsched.agents.codex import CODEX_OUTPUT_SCHEMA
from subsched.agents.isolation import IsolationGitContext
from subsched.agents.process import run_process_group
from subsched.audit_cache import materialize_audit_cache
from subsched.audit_github import AuditTransportIsolationError, fetch_github_cache
from subsched.audit_inventory import canonical_inventory
from subsched.audit_locked_inventory import AuditIsolationError, _read, derive_locked_inventory
from subsched.audit_provenance import AuditPolicy, AuthenticatedCache
from subsched.config import IndependentAuditConfig
from subsched.plan_review import PLAN_REVIEW_OUTPUT_SCHEMA


class AuditUnavailable(ValueError):
    """The external audit is missing, untrusted, stale or for a different target."""

    def __init__(self, *, cleanup_confirmed: bool = True) -> None:
        super().__init__("independent dependency audit is unavailable or untrusted")
        self.cleanup_confirmed = cleanup_confirmed


@dataclass(frozen=True, slots=True)
class PreparedAudit:
    path: Path
    evidence: AuthenticatedCache

    def is_fresh(self, now: datetime) -> bool:
        return self.evidence.is_fresh(now)


def _is_canonical_legacy_schema(worktree: Path, entry: str) -> bool:
    """Recognize only exact untracked Scheduler artifacts, without following links."""
    schemas = {
        "?? .ai/codex-output.schema.json": json.dumps(CODEX_OUTPUT_SCHEMA, indent=2) + "\n",
        "?? .ai/plan-review-output.schema.json": json.dumps(PLAN_REVIEW_OUTPUT_SCHEMA, indent=2),
    }
    expected = schemas.get(entry)
    if expected is None:
        return False
    if os.open not in os.supports_dir_fd or any(
        not hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    ):
        return False
    content = expected.encode("utf-8")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        with ExitStack() as handles:
            directory = os.open(worktree.anchor, flags)
            handles.callback(os.close, directory)
            for component in (*worktree.parts[1:], ".ai"):
                directory = os.open(component, flags, dir_fd=directory)
                handles.callback(os.close, directory)
            descriptor = os.open(
                entry.removeprefix("?? .ai/"),
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
            handles.callback(os.close, descriptor)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return False
            with os.fdopen(descriptor, "rb", closefd=False) as schema:
                return schema.read(len(content) + 1) == content
    except (OSError, NotImplementedError):
        return False


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
    # Git omits special files (for example FIFOs) from untracked status.
    for name in ("codex-output.schema.json", "plan-review-output.schema.json"):
        try:
            mode = (cwd / ".ai" / name).lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("worktree must be clean before auditing")
    status = execute((*snapshot_prefix, "status", "--porcelain=v1", "--untracked-files=all", "-z"))
    if status and (
        not status.endswith("\0")
        or not all(_is_canonical_legacy_schema(cwd, entry) for entry in status.split("\0")[:-1])
    ):
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
    fetch: Callable[..., AuthenticatedCache | None] = fetch_github_cache,
    now: datetime | None = None,
) -> PreparedAudit:
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
            now,
            inventory,
            gh=gh_executable,
        )
        if (
            not isinstance(cache, AuthenticatedCache)
            or hashlib.sha256(
                _checked_lock(worktree, git_context, cache_root.parent, runner)
            ).hexdigest()
            != target["lock_sha256"]
        ):
            raise ValueError("audit evidence is unavailable")
        materialize_audit_cache(cache_root, cache, inventory)
        if not cache.is_fresh(now or datetime.now(UTC)):
            raise ValueError("audit evidence expired before cache handoff")
        return PreparedAudit(cache_root, cache)
    except (AuditIsolationError, AuditTransportIsolationError) as error:
        raise AuditUnavailable(cleanup_confirmed=False) from error
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        raise AuditUnavailable() from error
