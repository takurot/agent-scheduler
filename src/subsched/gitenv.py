from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

RunCommand = Callable[..., subprocess.CompletedProcess[str]]

# Environment variables documented in `git`'s ENVIRONMENT VARIABLES section that override
# git's normal repository discovery. Any of these, if inherited from a parent process, make
# git silently ignore an explicit `-C <path>`/`cwd=` argument and operate on a different
# repository instead. Stripping them from every subsched-invoked git call is defense-in-depth
# hardening related to issue #147 (a leaked GIT_DIR was investigated there and not confirmed
# as the actual root cause of that incident, but this class of override is a real, documented
# git behavior worth foreclosing regardless).
GIT_LOCATION_OVERRIDE_VARS: frozenset[str] = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_CEILING_DIRECTORIES",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
    }
)


def git_safe_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment safe to pass to a `git -C <path>`/`cwd=<path>` subprocess call.

    Strips variables that override git's repository discovery so an explicit path argument
    cannot be silently redirected to an unrelated repository by inherited environment state.
    Defaults to a copy of the current process environment when `base` is not given.
    """
    source = dict(os.environ) if base is None else dict(base)
    for name in GIT_LOCATION_OVERRIDE_VARS:
        source.pop(name, None)
    return source


def ensure_git_exclude(
    repo_or_worktree: Path,
    pattern: str = ".ai/",
    *,
    run_cmd: RunCommand | None = None,
) -> Path:
    """Ensure `pattern` is present in the git repository or worktree's `info/exclude`.

    Resolves the exact `info/exclude` file via `git rev-parse --git-path info/exclude`
    so that linked worktrees, bare repositories, and standard repositories are all
    handled correctly. Appends `pattern` idempotently if not already present.
    """
    runner = run_cmd or subprocess.run
    exclude_path: Path | None = None
    try:
        res = runner(
            ["git", "-C", str(repo_or_worktree), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True,
            text=True,
            check=False,
            env=git_safe_env(),
        )
        if res.returncode == 0 and res.stdout.strip():
            raw = Path(res.stdout.strip())
            exclude_path = raw if raw.is_absolute() else (repo_or_worktree / raw).resolve()
    except OSError:
        exclude_path = None

    if exclude_path is None:
        if (repo_or_worktree / "info").is_dir() or (repo_or_worktree / "HEAD").is_file():
            exclude_path = repo_or_worktree / "info" / "exclude"
        else:
            exclude_path = repo_or_worktree / ".git" / "info" / "exclude"

    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    current_content = ""
    if exclude_path.exists():
        try:
            current_content = exclude_path.read_text(encoding="utf-8")
        except OSError:
            current_content = ""

    lines = [line.strip() for line in current_content.splitlines()]
    clean_pattern = pattern.strip()
    variants = {
        clean_pattern,
        clean_pattern.rstrip("/"),
        f"/{clean_pattern.lstrip('/')}",
        f"/{clean_pattern.lstrip('/').rstrip('/')}",
    }
    if not any(line in variants for line in lines):
        prefix = current_content
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        new_content = prefix + f"{clean_pattern}\n"
        exclude_path.write_text(new_content, encoding="utf-8")

    return exclude_path
