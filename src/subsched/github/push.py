from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from subsched.agents.process import redact_sensitive_command_audit
from subsched.gitenv import git_safe_env


class PushResultKind(StrEnum):
    SUCCESS = "SUCCESS"
    NON_FAST_FORWARD = "NON_FAST_FORWARD"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    DISALLOWED_BRANCH = "DISALLOWED_BRANCH"
    FAILURE = "FAILURE"


@dataclass(frozen=True, slots=True)
class PushResult:
    kind: PushResultKind
    output: str
    branch: str


def validate_branch_name(branch: str) -> bool:
    """Validate task branch conforms to safe naming format."""
    if branch in {"main", "master", "develop", "trunk", "release"}:
        return False
    return bool(re.fullmatch(r"[a-zA-Z0-9_/-]+", branch))


def validate_remote_name(remote: str) -> bool:
    """Validate remote name does not start with dash and contains only allowed characters."""
    return not remote.startswith("-") and bool(re.fullmatch(r"[a-zA-Z0-9._/-]+", remote))


def push_task_branch(
    worktree_dir: Path,
    branch_name: str,
    remote: str = "origin",
    env: dict[str, str] | None = None,
    timeout_seconds: float = 60.0,
) -> PushResult:
    """Push task branch to remote safely with explicit validations."""
    if not validate_branch_name(branch_name):
        return PushResult(
            kind=PushResultKind.DISALLOWED_BRANCH,
            output=f"refusing to push to disallowed or invalid branch: {branch_name}",
            branch=branch_name,
        )
    if not validate_remote_name(remote):
        return PushResult(
            kind=PushResultKind.FAILURE,
            output=f"refusing to push to invalid remote: {remote}",
            branch=branch_name,
        )

    argv = ("git", "push", "-u", "--", remote, f"HEAD:refs/heads/{branch_name}")
    try:
        res = subprocess.run(
            argv,
            cwd=str(worktree_dir),
            env=git_safe_env(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return PushResult(
            kind=PushResultKind.FAILURE,
            output=f"push execution failed: {err}",
            branch=branch_name,
        )

    combined = f"{res.stdout}\n{res.stderr}"
    redacted = redact_sensitive_command_audit(tuple(combined.splitlines()))
    clean_output = "\n".join(redacted)

    if res.returncode == 0:
        return PushResult(kind=PushResultKind.SUCCESS, output=clean_output, branch=branch_name)

    lower_out = clean_output.casefold()
    if "[rejected]" in lower_out or "non-fast-forward" in lower_out:
        return PushResult(
            kind=PushResultKind.NON_FAST_FORWARD, output=clean_output, branch=branch_name
        )
    if "permission denied" in lower_out or "403" in lower_out or "access denied" in lower_out:
        return PushResult(
            kind=PushResultKind.PERMISSION_DENIED, output=clean_output, branch=branch_name
        )

    return PushResult(kind=PushResultKind.FAILURE, output=clean_output, branch=branch_name)
