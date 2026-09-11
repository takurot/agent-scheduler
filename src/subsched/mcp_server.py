"""Model Context Protocol (MCP) server interface for subsched (#250).

Exposes subsched's scheduler state and controls as MCP tools/resources so an AI
coding assistant (Claude Desktop, Cursor, Antigravity/Gemini) can drive subsched from
another repository without prior knowledge of CLI flags. Every tool is a thin,
directly-testable wrapper around the same `JsonStateStore`/`Scheduler` primitives the
CLI uses, so state mutations still go through `JsonStateStore.lock()` and every
existing invariant (subscription-only billing, native opt-in, worktree preservation)
is enforced exactly once, not re-implemented here.

Native worker execution is never run in-process: `trigger_dispatch` launches
`subsched run` as a detached background subprocess (the same CLI entrypoint, with the
same safety gates) and returns immediately, so a long-running task (minutes to hours)
never blocks the MCP event loop or triggers a client timeout.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from subsched.agents.native import NativeWorker
from subsched.config import ConfigError
from subsched.gitenv import git_safe_env
from subsched.github.issues import GitHubCliError, GitHubIssueSource
from subsched.handoff import parse_semantic_handoff
from subsched.init import InitError, build_scaffold_plan, write_scaffold_plan
from subsched.metrics import calculate_metrics
from subsched.models import Task, TaskState
from subsched.router import Router
from subsched.scheduler import Scheduler
from subsched.storage import (
    JsonStateStore,
    SchedulerLockError,
    StateCorruptionError,
    find_repository_root,
)

GUIDELINES = """# subsched Agent Guidelines

## TDD Workflow
1. Write a failing test first; confirm it fails for the right reason.
2. Implement the minimal code to make it pass.
3. Do not edit tests to make them pass -- fix the implementation instead.
4. Re-run the full verification suite before finishing.

## Handoff Standard (.ai/handoffs/<issue>.md)
Every handoff must keep all 8 section headers, in order, exactly as named:
`## Goal`, `## Current Plan`, `## Completed`, `## Current Work`, `## Decisions`,
`## Known Broken State`, `## Next Action`, `## Timestamp`. `## Timestamp` must contain
only a pure ISO 8601 string, always advanced past the previous value.

## Commits & Pull Requests
- Use Conventional Commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`).
- Never use GitHub auto-close keywords (`Fixes #N`, `Closes #N`, `Resolves #N`, any case
  or inflection) in commit messages or PR descriptions -- use `issue #N` instead.

## Safety Invariants
- Subscription-only: never enable metered usage or API fallback.
- Never `git reset --hard` or broadly `git clean` a worktree.
- Fail closed when billing, authentication, capacity, or path boundaries are unknown.
"""


class McpToolError(RuntimeError):
    """Raised by an MCP tool when a request cannot be safely fulfilled."""


def resolve_repository(repository_path: str | None) -> Path:
    """Resolve and validate `repository_path` -- untrusted MCP tool input -- to a git
    repository root, failing closed instead of silently operating on an unexpected
    directory. Falls back to the current working directory (matching the CLI's
    `--repository` auto-detection) when omitted.
    """
    candidate = Path(repository_path).expanduser() if repository_path else Path.cwd()
    if repository_path is not None:
        if candidate.is_symlink():
            raise McpToolError(f"repository_path must not be a symlink: {candidate}")
        if not candidate.is_dir():
            raise McpToolError(
                f"repository_path does not exist or is not a directory: {candidate}"
            )
    return find_repository_root(candidate)


def _store_for(repository_path: str | None) -> JsonStateStore:
    return JsonStateStore(resolve_repository(repository_path))


def _task_to_summary(task: Task) -> dict[str, Any]:
    return {
        "issue_number": task.issue_number,
        "title": task.title,
        "status": task.status.value,
        "current_agent": task.current_agent,
        "pr": task.pr,
        "needs_human_reason": task.needs_human_reason,
    }


def get_status(repository_path: str | None = None, verbose: bool = False) -> dict[str, Any]:
    """Current queue status breakdown, task lists, and capacity/cooldown state."""
    store = _store_for(repository_path)
    try:
        tasks = store.load_tasks()
        capacities = store.load_capacities()
        paused = store.is_paused()
    except StateCorruptionError as error:
        raise McpToolError(f"scheduler state error: {error}") from error

    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.status.value] = counts.get(task.status.value, 0) + 1

    result: dict[str, Any] = {
        "paused": paused,
        "task_counts": counts,
        "capacities": [capacity.to_dict() for capacity in capacities],
    }
    if verbose:
        result["tasks"] = [_task_to_summary(task) for task in tasks]
    return result


def inspect_task(issue_number: int, repository_path: str | None = None) -> dict[str, Any]:
    """Full task details, parsed semantic handoff, and recent worktree commits."""
    store = _store_for(repository_path)
    try:
        tasks = store.load_tasks()
    except StateCorruptionError as error:
        raise McpToolError(f"scheduler state error: {error}") from error

    matches = [task for task in tasks if task.issue_number == issue_number]
    if not matches:
        raise McpToolError(f"issue #{issue_number} is not in scheduler state")
    task = matches[0]

    result: dict[str, Any] = task.to_dict()
    result["handoff"] = None
    result["recent_commits"] = ()

    if task.worktree:
        worktree_dir = Path(task.worktree)
        handoff_file = worktree_dir / ".ai" / "handoffs" / f"{issue_number}.md"
        if handoff_file.is_file() and not handoff_file.is_symlink():
            content = handoff_file.read_text(encoding="utf-8")
            parsed = parse_semantic_handoff(content)
            if parsed is not None:
                result["handoff"] = {
                    "goal": parsed.goal,
                    "completed": parsed.completed,
                    "current_work": parsed.current_work,
                    "decisions": parsed.decisions,
                    "broken_state": parsed.broken_state,
                    "next_action": parsed.next_action,
                    "timestamp": parsed.timestamp,
                }
        if worktree_dir.is_dir():
            try:
                log = subprocess.run(
                    ["git", "-C", str(worktree_dir), "log", "-n", "5", "--oneline"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                    env=git_safe_env(),
                )
                if log.returncode == 0:
                    result["recent_commits"] = tuple(
                        line for line in log.stdout.splitlines() if line
                    )
            except (OSError, subprocess.TimeoutExpired):
                pass
    return result


def queue_issues(
    repository_path: str | None = None,
    issues: str | None = None,
    label: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Discover open issues from GitHub and persist them to the durable queue.

    `dry_run=True` reports what would be discovered without touching scheduler
    state; `dry_run=False` persists new tasks exactly as `subsched run` does.
    """
    store = _store_for(repository_path)
    try:
        cfg_repo = _github_repo(store.state_dir.parent)
    except McpToolError:
        raise
    try:
        open_issues = GitHubIssueSource().list_open(cfg_repo, label=label)
    except GitHubCliError as error:
        raise McpToolError(f"GitHub discovery failed: {error}") from error

    requested: frozenset[int] | None = None
    if issues is not None and issues != "all-open":
        try:
            requested = frozenset(int(item) for item in issues.split(","))
        except ValueError as error:
            raise McpToolError("issues must be 'all-open' or comma-separated integers") from error

    discovered = tuple(
        issue for issue in open_issues if requested is None or issue.number in requested
    )

    if dry_run:
        try:
            existing = {task.issue_number for task in store.load_tasks()}
        except StateCorruptionError as error:
            raise McpToolError(f"scheduler state error: {error}") from error
        new_count = sum(1 for issue in discovered if issue.number not in existing)
        return {"discovered": len(discovered), "would_queue": new_count, "dry_run": True}

    try:
        scheduler = Scheduler(
            store=store,
            router=Router(()),
            worker=NativeWorker(),
            worktree_root=store.worktrees_dir,
        )
        before = len(scheduler.tasks)
        scheduler.discover(discovered)
        added = len(scheduler.tasks) - before
    except (SchedulerLockError, StateCorruptionError, ValueError) as error:
        raise McpToolError(f"queueing failed: {error}") from error

    return {"discovered": len(discovered), "queued": added, "dry_run": False}


def _github_repo(repository: Path) -> str:
    from subsched.config import SchedulerConfig, load_config

    config_path = repository / "subsched.yaml"
    cfg: SchedulerConfig
    try:
        cfg = load_config(config_path) if config_path.is_file() else SchedulerConfig()
    except ConfigError as error:
        raise McpToolError(f"invalid subsched.yaml: {error}") from error
    if cfg.github.repo is None:
        raise McpToolError("github.repo is not configured in subsched.yaml")
    return cfg.github.repo


def trigger_dispatch(
    repository_path: str | None = None,
    issues: str | None = None,
    allow_native: bool = False,
    subscription_billing_verified: bool = False,
) -> dict[str, Any]:
    """Launch `subsched run` as a detached background subprocess and return
    immediately -- native worker execution can take minutes to hours and must never
    block an MCP tool call. `allow_native` and `subscription_billing_verified` default
    to False (fail closed): the caller must explicitly opt in to native execution and
    confirm subscription billing, matching the CLI's own gates.
    """
    repository = resolve_repository(repository_path)
    if allow_native and not subscription_billing_verified:
        raise McpToolError(
            "native execution requires subscription_billing_verified=True; refusing "
            "to dispatch unverified billing"
        )
    _github_repo(repository)

    argv = [sys.executable, "-m", "subsched", "--repository", str(repository), "run"]
    if issues is not None:
        argv.extend(["--issues", issues])
    if allow_native:
        argv.extend(["--allow-native", "--subscription-billing-verified"])
    else:
        argv.append("--dry-run")

    store = JsonStateStore(repository)
    store.init_directories()
    log_path = store.runtime_dir / "mcp_dispatch.log"
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            argv,
            cwd=repository,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return {
        "status": "dispatched",
        "pid": process.pid,
        "allow_native": allow_native,
        "log_path": str(log_path),
    }


def init_repo(
    repository_path: str | None = None,
    force: bool = False,
    generate_agent_instructions: bool = True,
) -> dict[str, Any]:
    """Scaffold `subsched.yaml`, `AGENTS.md`, and `CLAUDE.md` for a repository."""
    repository = resolve_repository(repository_path)
    plan = build_scaffold_plan(
        repository,
        repo_override=None,
        include_agents_md=generate_agent_instructions,
        include_claude_md=generate_agent_instructions,
    )
    try:
        written = write_scaffold_plan(plan, force=force)
    except InitError as error:
        raise McpToolError(str(error)) from error
    return {
        "stack": plan.stack.name,
        "repo": plan.repo,
        "written": [str(path) for path in written],
    }


def resolve_needs_human(
    issue_number: int,
    resolution_notes: str | None = None,
    repository_path: str | None = None,
) -> dict[str, Any]:
    """Reset a NEEDS_HUMAN task back to READY after human/agent remediation."""
    store = _store_for(repository_path)
    try:
        with store.lock():
            tasks = store.load_tasks()
            matches = [task for task in tasks if task.issue_number == issue_number]
            if not matches:
                raise McpToolError(f"issue #{issue_number} is not in scheduler state")
            task = matches[0]
            if task.status is not TaskState.NEEDS_HUMAN:
                raise McpToolError(
                    f"issue #{issue_number} is not in NEEDS_HUMAN (status: {task.status.value})"
                )
            reason = resolution_notes or "resolved via subsched_resolve_needs_human"
            replacement = task.transition(TaskState.READY, reason=reason)
            updated = tuple(
                replacement if item.issue_number == issue_number else item for item in tasks
            )
            store.save_tasks(updated, paused=store.is_paused())
    except (SchedulerLockError, StateCorruptionError, ValueError) as error:
        raise McpToolError(f"resolution failed: {error}") from error
    return {"issue_number": issue_number, "status": TaskState.READY.value}


def cancel_task(issue_number: int, repository_path: str | None = None) -> dict[str, Any]:
    """Cancel one task while preserving its worktree and handoff files."""
    store = _store_for(repository_path)
    try:
        with store.lock():
            tasks = store.load_tasks()
            matches = [task for task in tasks if task.issue_number == issue_number]
            if not matches:
                raise McpToolError(f"issue #{issue_number} is not in scheduler state")
            task = matches[0]
            if task.status is not TaskState.CANCELLED:
                try:
                    replacement = task.transition(TaskState.CANCELLED)
                except ValueError as error:
                    raise McpToolError(
                        f"issue #{issue_number} cannot be cancelled from {task.status.value}"
                    ) from error
                updated = tuple(
                    replacement if item.issue_number == issue_number else item for item in tasks
                )
                store.save_tasks(updated, paused=store.is_paused())
    except (SchedulerLockError, StateCorruptionError) as error:
        raise McpToolError(f"cancellation failed: {error}") from error
    return {
        "issue_number": issue_number,
        "status": TaskState.CANCELLED.value,
        "worktree_preserved": True,
    }


def control(
    action: Literal["pause", "resume"], repository_path: str | None = None
) -> dict[str, Any]:
    """Pause or resume new dispatches; running work is unaffected."""
    store = _store_for(repository_path)
    try:
        store.set_paused(action == "pause")
    except (SchedulerLockError, StateCorruptionError) as error:
        raise McpToolError(f"state error: {error}") from error
    return {"paused": store.is_paused()}


def get_metrics(repository_path: str | None = None) -> dict[str, Any]:
    """Productivity, Reliability, and Capacity metrics."""
    store = _store_for(repository_path)
    try:
        tasks = store.load_tasks()
    except StateCorruptionError as error:
        raise McpToolError(f"scheduler state error: {error}") from error
    return calculate_metrics(tasks).to_dict()


def get_queue_resource(repository_path: str | None = None) -> dict[str, Any]:
    """Real-time snapshot of tasks and queue state (`subsched://queue`)."""
    store = _store_for(repository_path)
    try:
        tasks = store.load_tasks()
        paused = store.is_paused()
    except StateCorruptionError as error:
        raise McpToolError(f"scheduler state error: {error}") from error
    return {"paused": paused, "tasks": [task.to_dict() for task in tasks]}


def get_capacities_resource(repository_path: str | None = None) -> dict[str, Any]:
    """Real-time snapshot of provider capacities (`subsched://capacities`)."""
    store = _store_for(repository_path)
    try:
        capacities = store.load_capacities()
    except StateCorruptionError as error:
        raise McpToolError(f"scheduler state error: {error}") from error
    return {"capacities": [capacity.to_dict() for capacity in capacities]}


def get_task_handoff_resource(issue_number: int, repository_path: str | None = None) -> str:
    """Markdown content of a task's semantic handoff (`subsched://tasks/{issue}/handoff`)."""
    store = _store_for(repository_path)
    try:
        tasks = store.load_tasks()
    except StateCorruptionError as error:
        raise McpToolError(f"scheduler state error: {error}") from error
    matches = [task for task in tasks if task.issue_number == issue_number]
    if not matches or not matches[0].worktree:
        raise McpToolError(f"no handoff available for issue #{issue_number}")
    handoff_file = Path(matches[0].worktree) / ".ai" / "handoffs" / f"{issue_number}.md"
    if not handoff_file.is_file() or handoff_file.is_symlink():
        raise McpToolError(f"no handoff available for issue #{issue_number}")
    return handoff_file.read_text(encoding="utf-8")


def get_guidelines_resource() -> str:
    """Reference guidelines for coding agents (`subsched://guidelines`)."""
    return GUIDELINES


@dataclass(frozen=True, slots=True)
class ServerOptions:
    default_repository: Path


def build_server(options: ServerOptions) -> Any:
    """Construct the `FastMCP` stdio server, wiring every tool/resource/prompt above.

    Imported lazily -- `mcp` is an optional extra (`pip install agent-scheduler[mcp]`)
    -- so importing `subsched.mcp_server` for its pure functions (e.g. in tests) never
    requires the `mcp` package to be installed.
    """
    from mcp.server.fastmcp import FastMCP

    default_repository = str(options.default_repository)
    server = FastMCP("subsched")

    def _default(repository_path: str | None) -> str | None:
        return repository_path if repository_path is not None else default_repository

    @server.tool(name="subsched_get_status")
    def _get_status(repository_path: str | None = None, verbose: bool = False) -> dict[str, Any]:
        return get_status(_default(repository_path), verbose)

    @server.tool(name="subsched_inspect_task")
    def _inspect_task(issue_number: int, repository_path: str | None = None) -> dict[str, Any]:
        return inspect_task(issue_number, _default(repository_path))

    @server.tool(name="subsched_queue_issues")
    def _queue_issues(
        issues: str | None = None,
        label: str | None = None,
        dry_run: bool = False,
        repository_path: str | None = None,
    ) -> dict[str, Any]:
        return queue_issues(_default(repository_path), issues, label, dry_run)

    @server.tool(name="subsched_trigger_dispatch")
    def _trigger_dispatch(
        issues: str | None = None,
        allow_native: bool = False,
        subscription_billing_verified: bool = False,
        repository_path: str | None = None,
    ) -> dict[str, Any]:
        return trigger_dispatch(
            _default(repository_path), issues, allow_native, subscription_billing_verified
        )

    @server.tool(name="subsched_init_repo")
    def _init_repo(
        repository_path: str | None = None,
        force: bool = False,
        generate_agent_instructions: bool = True,
    ) -> dict[str, Any]:
        return init_repo(_default(repository_path), force, generate_agent_instructions)

    @server.tool(name="subsched_resolve_needs_human")
    def _resolve_needs_human(
        issue_number: int,
        resolution_notes: str | None = None,
        repository_path: str | None = None,
    ) -> dict[str, Any]:
        return resolve_needs_human(issue_number, resolution_notes, _default(repository_path))

    @server.tool(name="subsched_cancel_task")
    def _cancel_task(issue_number: int, repository_path: str | None = None) -> dict[str, Any]:
        return cancel_task(issue_number, _default(repository_path))

    @server.tool(name="subsched_control")
    def _control(
        action: Literal["pause", "resume"], repository_path: str | None = None
    ) -> dict[str, Any]:
        return control(action, _default(repository_path))

    @server.tool(name="subsched_get_metrics")
    def _get_metrics(repository_path: str | None = None) -> dict[str, Any]:
        return get_metrics(_default(repository_path))

    @server.resource("subsched://queue")
    def _queue_resource() -> dict[str, Any]:
        return get_queue_resource(default_repository)

    @server.resource("subsched://capacities")
    def _capacities_resource() -> dict[str, Any]:
        return get_capacities_resource(default_repository)

    @server.resource("subsched://tasks/{issue}/handoff")
    def _handoff_resource(issue: str) -> str:
        try:
            issue_number = int(issue)
        except ValueError as error:
            raise McpToolError(f"invalid issue number: {issue}") from error
        return get_task_handoff_resource(issue_number, default_repository)

    @server.resource("subsched://guidelines")
    def _guidelines_resource() -> str:
        return get_guidelines_resource()

    @server.prompt(name="triage_task")
    def _triage_task(issue_number: int) -> str:
        return (
            f"Investigate issue #{issue_number}, which is in NEEDS_HUMAN state. Use "
            "subsched_inspect_task to review its handoff, decisions, and recent commits, "
            "diagnose the root cause, and either fix it directly in its worktree or "
            "explain why it cannot be safely resolved automatically. Only call "
            "subsched_resolve_needs_human once the underlying problem is actually fixed."
        )

    @server.prompt(name="bootstrap_repo")
    def _bootstrap_repo() -> str:
        return (
            "Assess this repository's language/tooling stack and GitHub remote, then call "
            "subsched_init_repo to scaffold subsched.yaml, AGENTS.md, and CLAUDE.md. Review "
            "the generated subsched.yaml's verification commands and github.include_labels "
            "before running subsched_queue_issues."
        )

    return server
