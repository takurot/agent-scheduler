"""Scaffolding for `subsched init`: detects a project's stack, resolves its GitHub repo
slug, and renders `subsched.yaml`, `AGENTS.md`, and `CLAUDE.md` so a new repository can
adopt subsched without hand-writing configuration and agent instructions from scratch (#258).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from subsched.config import ConfigError, validate_repo
from subsched.gitenv import git_safe_env

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class InitError(ValueError):
    """Raised when `subsched init` cannot safely proceed."""


@dataclass(frozen=True, slots=True)
class StackDetection:
    name: str
    verification_commands: tuple[str, ...]


def _detect_python_stack(path: Path) -> StackDetection:
    if (path / "uv.lock").exists():
        commands = ["uv run pytest", "uv run ruff check ."]
        commands.append(f"uv run mypy {'src' if (path / 'src').is_dir() else '.'}")
        return StackDetection("python-uv", tuple(commands))
    if (path / "poetry.lock").exists():
        return StackDetection("python-poetry", ("poetry run pytest", "poetry run ruff check ."))
    return StackDetection("python", ("pytest", "ruff check ."))


def _detect_node_stack(path: Path) -> StackDetection:
    if (path / "pnpm-lock.yaml").exists():
        manager = "pnpm"
    elif (path / "yarn.lock").exists():
        manager = "yarn"
    else:
        manager = "npm"

    scripts: dict[str, object] = {}
    try:
        raw = json.loads((path / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    if isinstance(raw, dict) and isinstance(raw.get("scripts"), dict):
        scripts = raw["scripts"]

    commands: list[str] = []
    if "test" in scripts:
        commands.append("npm test" if manager == "npm" else f"{manager} test")
    if "lint" in scripts:
        commands.append("npm run lint" if manager == "npm" else f"{manager} lint")
    if not commands:
        commands.append("npm test" if manager == "npm" else f"{manager} test")
    return StackDetection("node", tuple(commands))


def detect_stack(path: Path) -> StackDetection:
    """Detect a project's language/tooling stack from files present at `path`.

    Checked in a fixed priority order (Python, Node, Go, Rust) since a repository may
    match more than one marker (e.g. a Python backend with a `package.json` frontend
    tool); falls back to the generic pytest/ruff template when nothing matches.
    """
    python_markers = ("pyproject.toml", "uv.lock", "poetry.lock", "Pipfile", "requirements.txt")
    if any((path / marker).exists() for marker in python_markers):
        return _detect_python_stack(path)
    if (path / "go.mod").exists():
        return StackDetection("go", ("go test ./...", "golangci-lint run"))
    if (path / "Cargo.toml").exists():
        return StackDetection("rust", ("cargo test", "cargo clippy"))
    if (path / "package.json").exists():
        return _detect_node_stack(path)
    return StackDetection("generic", ("pytest", "ruff check ."))


_GITHUB_HTTPS_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<name>[^/\s]+?)(?:\.git)?/?$"
)
_GITHUB_SSH_RE = re.compile(
    r"^git@github\.com:(?P<owner>[^/\s]+)/(?P<name>[^/\s]+?)(?:\.git)?$"
)


def _parse_github_remote(url: str) -> str | None:
    url = url.strip()
    for pattern in (_GITHUB_HTTPS_RE, _GITHUB_SSH_RE):
        match = pattern.match(url)
        if match:
            return f"{match.group('owner')}/{match.group('name')}"
    return None


def _safe_validated_repo(candidate: str | None) -> str | None:
    if candidate is None:
        return None
    try:
        return validate_repo(candidate)
    except ConfigError:
        return None


def resolve_github_repo(path: Path, *, run: RunCommand | None = None) -> str | None:
    """Best-effort resolution of `owner/name` for `path`'s GitHub repository.

    Tries the local `origin` git remote first, falling back to `gh repo view`. Returns
    None (never raises) when neither source yields a usable, validated repo slug, so
    callers can fail closed or prompt the user instead of writing an unverified guess.
    """
    runner = run or subprocess.run

    try:
        remote_result = runner(
            ["git", "remote", "get-url", "origin"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=git_safe_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        remote_result = None
    if remote_result is not None and remote_result.returncode == 0:
        repo = _safe_validated_repo(_parse_github_remote(remote_result.stdout))
        if repo is not None:
            return repo

    try:
        gh_result = runner(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=git_safe_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if gh_result.returncode != 0:
        return None
    return _safe_validated_repo(gh_result.stdout.strip() or None)


def render_subsched_yaml(*, repo: str | None, verification_commands: tuple[str, ...]) -> str:
    if repo is not None:
        repo_lines = f"  repo: {repo}\n"
    else:
        repo_lines = (
            "  # Could not auto-detect the GitHub repository; set it before running\n"
            "  # `subsched config validate` or `subsched run`.\n"
            "  # repo: owner/name\n"
        )
    commands_block = "\n".join(f"    - {command}" for command in verification_commands)
    return f"""github:
{repo_lines}  include_labels:
    - ai-ready
  exclude_labels:
    - blocked
    - human-only
    - security-sensitive
  completion:
    create_pr: true
    # When true, appends "Closes #<issue>" to PR body so the issue automatically closes
    # upon PR merge.
    close_issue: false

# Supported agents: claude, codex. At least one agent must remain enabled.
agents:
  claude:
    enabled: true
    priority: 100
  codex:
    enabled: true
    priority: 90

execution:
  concurrency: 1

verification:
  commands:
{commands_block}

billing:
  api_fallback: false
  metered_usage: false
  unknown_mode: disable
"""


def render_agent_instructions(*, verification_commands: tuple[str, ...]) -> str:
    """Shared content for both `AGENTS.md` and `CLAUDE.md` (this repository keeps its
    own two such files in sync as plain duplicates rather than a cross-file import, so
    the scaffolded files follow the same convention)."""
    commands_block = "\n".join(f"- `{command}`" for command in verification_commands)
    return f"""# Agent Instructions

This repository is orchestrated by [subsched](https://github.com/), a subscription-only
scheduler for AI coding agents. Follow these guidelines on every task.

## Simplicity First

Write the minimum code that solves the problem. No speculative abstractions, no
unrequested configurability, no error handling for impossible internal states.

## Surgical Changes

Touch only what the task requires. Do not refactor unrelated code, reformat files, or
"improve" adjacent comments -- every changed line should trace back to the task.

## Test-Driven Development

1. Write a failing test first.
2. Run it and confirm it fails for the right reason.
3. Implement the minimal code to make it pass.
4. Do not edit tests to make them pass -- fix the implementation instead.
5. Re-run the full verification suite before finishing.

## Verification

Run these commands before considering a task complete:

{commands_block}

## Commits & Pull Requests

- Use Conventional Commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`).
- Never use GitHub auto-close keywords (`Fixes #N`, `Closes #N`, `Resolves #N`, any case
  or inflection) in commit messages or PR descriptions -- use a plain reference such as
  `issue #N` instead. subsched manages issue closure separately, after manual review.
- Do not push or open pull requests yourself unless explicitly instructed; the scheduler
  handles that once verification passes.
"""


@dataclass(frozen=True, slots=True)
class ScaffoldFile:
    path: Path
    content: str
    exists: bool


@dataclass(frozen=True, slots=True)
class ScaffoldPlan:
    repo: str | None
    stack: StackDetection
    files: tuple[ScaffoldFile, ...]


def build_scaffold_plan(
    path: Path,
    *,
    repo_override: str | None,
    include_agents_md: bool,
    include_claude_md: bool,
    run: RunCommand | None = None,
) -> ScaffoldPlan:
    repo = repo_override if repo_override is not None else resolve_github_repo(path, run=run)
    stack = detect_stack(path)

    files: list[ScaffoldFile] = []
    yaml_path = path / "subsched.yaml"
    yaml_content = render_subsched_yaml(
        repo=repo, verification_commands=stack.verification_commands
    )
    files.append(ScaffoldFile(path=yaml_path, content=yaml_content, exists=yaml_path.exists()))

    instructions = render_agent_instructions(verification_commands=stack.verification_commands)
    if include_agents_md:
        agents_path = path / "AGENTS.md"
        files.append(
            ScaffoldFile(path=agents_path, content=instructions, exists=agents_path.exists())
        )
    if include_claude_md:
        claude_path = path / "CLAUDE.md"
        files.append(
            ScaffoldFile(path=claude_path, content=instructions, exists=claude_path.exists())
        )

    return ScaffoldPlan(repo=repo, stack=stack, files=tuple(files))


def write_scaffold_plan(plan: ScaffoldPlan, *, force: bool) -> tuple[Path, ...]:
    """Write every file in `plan` to disk, failing closed before writing anything if any
    target already exists and `force` was not given."""
    if not force:
        conflicts = [f.path for f in plan.files if f.exists]
        if conflicts:
            names = ", ".join(str(p) for p in conflicts)
            raise InitError(f"refusing to overwrite existing file(s) without --force: {names}")

    written: list[Path] = []
    for file in plan.files:
        file.path.parent.mkdir(parents=True, exist_ok=True)
        file.path.write_text(file.content, encoding="utf-8")
        written.append(file.path)
    return tuple(written)
