from __future__ import annotations

import functools
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer

from subsched.agents.claude import ClaudeBillingMode, ClaudeExecutionPolicy
from subsched.agents.native import NativeWorker
from subsched.capacity.claude import ClaudeCapacitySensor
from subsched.capacity.codex import CodexCapacitySensor
from subsched.config import (
    ConfigError,
    SchedulerConfig,
    load_config,
    parse_natural_language_instruction,
    validate_repo,
)
from subsched.github.checks import fetch_pr_checks
from subsched.github.issues import GitHubCliError, GitHubIssueSource, diagnose_token
from subsched.github.pull_requests import check_merged_pr_for_issue
from subsched.models import TaskState
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler
from subsched.storage import (
    JsonStateStore,
    SchedulerLockError,
    StateCorruptionError,
    find_repository_root,
)
from subsched.structured_logger import StructuredLogger
from subsched.tasks.worktree import GitWorktreeAdapter, WorktreeAdapter, WorktreeError

app = typer.Typer(no_args_is_help=True, help="Subscription-aware coding agent scheduler")
RepositoryOption = Annotated[
    Path | None,
    typer.Option("--repository", hidden=True, file_okay=False, resolve_path=True),
]


class Context:
    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self.store = JsonStateStore(repository)


@app.callback()
def main(ctx: typer.Context, repository: RepositoryOption = None) -> None:
    # When --repository is not given explicitly, auto-detect the git repository root from the
    # current directory instead of using the cwd verbatim, so running from a subdirectory (e.g.
    # `src/`) resolves `.ai/` state to the repository root rather than a separate, empty tree.
    resolved_repository = (
        repository if repository is not None else find_repository_root(Path.cwd())
    )
    ctx.obj = Context(resolved_repository)


def _parse_issue_numbers(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(,\d+)*", value):
        raise typer.BadParameter("issues must be all-open or comma-separated positive integers")
    numbers = tuple(int(item) for item in value.split(","))
    if any(number <= 0 for number in numbers) or len(set(numbers)) != len(numbers):
        raise typer.BadParameter("issue numbers must be positive and unique")
    return numbers


@app.command()
def run(
    ctx: typer.Context,
    query: Annotated[
        str | None,
        typer.Argument(help="Natural language instruction (e.g. 'GitHubのopen issueをすべて実行')"),
    ] = None,
    repo: Annotated[str | None, typer.Option("--repo", help="GitHub owner/repository")] = None,
    label: Annotated[str | None, typer.Option("--label")] = None,
    issues: Annotated[str | None, typer.Option("--issues")] = None,
    config: Annotated[
        Path | None, typer.Option("--config", help="Path to YAML configuration file")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Discover and persist only; do not invoke workers")
    ] = False,
    allow_native: Annotated[
        bool, typer.Option("--allow-native", help="Explicitly enable native worker execution")
    ] = False,
    allow_rediscovery: Annotated[
        bool,
        typer.Option(
            "--allow-rediscovery",
            help=(
                "Skip the merged-PR check and treat every eligible open issue as READY "
                "even if it may already have a merged implementation PR (#146). Off by "
                "default: without this explicit override, a merged-but-still-open issue "
                "is excluded or escalated to NEEDS_HUMAN instead of being rediscovered."
            ),
        ),
    ] = False,
) -> None:
    """Discover issues and initialize the durable queue."""
    context: Context = ctx.obj
    try:
        cfg = load_config(config) if config is not None else SchedulerConfig()
    except ConfigError as error:
        raise typer.BadParameter(str(error), param_hint="--config") from error

    resolved_repo = repo
    resolved_label = label
    resolved_issues = issues

    if query is not None:
        try:
            intent = parse_natural_language_instruction(query)
            if intent.repo and resolved_repo is None:
                resolved_repo = intent.repo
            if intent.issues and resolved_issues is None and resolved_label is None:
                resolved_issues = intent.issues
            if intent.label and resolved_label is None and resolved_issues is None:
                resolved_label = intent.label
        except ConfigError as error:
            raise typer.BadParameter(str(error)) from error

    if resolved_repo is None:
        resolved_repo = cfg.github.repo

    if resolved_repo is None:
        raise typer.BadParameter(
            "missing required --repo option or config.github.repo", param_hint="--repo"
        )

    try:
        validate_repo(resolved_repo)
    except ConfigError as error:
        raise typer.BadParameter(str(error), param_hint="--repo") from error

    if resolved_label is not None and resolved_issues is not None:
        raise typer.BadParameter("select exactly one of --label or --issues")

    if resolved_label is None and resolved_issues is None:
        if cfg.github.mode == "all-open":
            resolved_issues = "all-open"
        elif cfg.github.mode == "label" and cfg.github.include_labels:
            resolved_label = cfg.github.include_labels[0]
        else:
            raise typer.BadParameter("select exactly one of --label or --issues")

    if not dry_run and not allow_native:
        typer.echo(
            "native workers require explicit opt-in (--allow-native) and doctor checks",
            err=True,
        )
        raise typer.Exit(2)

    if allow_native and not dry_run:
        missing_cmds = [
            cmd for cmd in ("git", "gh", "claude", "codex") if shutil.which(cmd) is None
        ]
        if missing_cmds:
            joined = ", ".join(missing_cmds)
            typer.echo(
                f"Native execution pre-flight doctor check failed; missing commands: {joined}",
                err=True,
            )
            raise typer.Exit(2)
        typer.echo("Pre-flight safety checks passed: subscription verified, API fallback disabled.")

    # push must reflect Scheduler._finalize_verified_task()'s actual gate: create_pr=false
    # takes the same no-git-writes-at-all path as push_enabled=False (see docs/SPEC.md
    # §40), so a misleading "push=True" here would contradict that.
    effective_push = not dry_run and cfg.github.completion.create_pr
    typer.echo(
        "Effective write policy: "
        f"native={allow_native and not dry_run}, "
        f"push={effective_push}, "
        f"create_pr={cfg.github.completion.create_pr}, "
        f"close_issue={cfg.github.completion.close_issue}"
    )

    requested: frozenset[int] | None = None
    if resolved_issues is not None and resolved_issues != "all-open":
        requested = frozenset(_parse_issue_numbers(resolved_issues))
    try:
        open_issues = GitHubIssueSource().list_open(resolved_repo, label=resolved_label)
    except GitHubCliError as error:
        typer.echo(f"GitHub discovery failed: {error}", err=True)
        raise typer.Exit(1) from error

    exclude_labels = frozenset(cfg.github.exclude_labels)
    discovered = tuple(
        issue for issue in open_issues if (requested is None or issue.number in requested)
    )
    if requested is not None:
        missing = requested - {issue.number for issue in open_issues}
        if missing:
            typer.echo("Requested issues are not open or were not found", err=True)
            raise typer.Exit(1)

    worktree_root = context.repository / ".ai" / "worktrees"
    worktree_adapter: WorktreeAdapter | None = None
    if not dry_run:
        try:
            worktree_adapter = GitWorktreeAdapter(context.repository, worktree_root)
        except WorktreeError as error:
            typer.echo(f"Worktree setup failed: {error}", err=True)
            raise typer.Exit(1) from error

    ci_checker = None
    if cfg.execution.ci_monitoring:
        ci_checker = functools.partial(fetch_pr_checks, repo=resolved_repo)

    merged_pr_checker = None
    if not allow_rediscovery:
        merged_pr_checker = functools.partial(check_merged_pr_for_issue, resolved_repo)

    try:
        scheduler = Scheduler(
            store=context.store,
            router=Router(
                AgentConfig(name, priority=settings.priority, enabled=settings.enabled)
                for name, settings in cfg.agents.items()
            ),
            worker=NativeWorker(
                agent_timeout_seconds=float(cfg.execution.agent_timeout_seconds),
                # #139: same tuple passed to the Scheduler's verification_commands=
                # below, so the worker prompt and the post-worker gate never diverge.
                verification_commands=cfg.verification.commands,
            ),
            worktree_root=worktree_root,
            worktree_adapter=worktree_adapter,
            # #138: was defined in config but never reached TaskQueue's dispatch-order
            # sort key, so label_scores appeared to be honored (config loaded without
            # error) but dispatch order stayed plain issue-number order regardless.
            label_scores=dict(cfg.queue.priority.label_scores),
            verification_commands=cfg.verification.commands,
            verification_timeout_seconds=float(cfg.verification.timeout_seconds),
            concurrency=cfg.execution.concurrency,
            max_agent_failures=cfg.execution.max_agent_failures,
            max_agent_switches=cfg.execution.max_agent_switches,
            max_tasks=cfg.execution.max_tasks_per_run,
            push_enabled=not dry_run,
            create_pr_enabled=cfg.github.completion.create_pr,
            repo=resolved_repo,
            structured_logger=StructuredLogger(context.store.runtime_dir / "scheduler.jsonl"),
            ci_checker=ci_checker,
            merged_pr_checker=merged_pr_checker,
        )
    except (ValueError, StateCorruptionError) as error:
        typer.echo(f"State error: {error}", err=True)
        raise typer.Exit(1) from error

    before_count = len(scheduler.tasks)
    try:
        scheduler.discover(discovered, exclude_labels=exclude_labels)
    except ValueError as error:
        typer.echo(f"Task limit exceeded ({cfg.execution.max_tasks_per_run})", err=True)
        raise typer.Exit(2) from error
    except (SchedulerLockError, StateCorruptionError) as error:
        typer.echo(f"State error: {error}", err=True)
        raise typer.Exit(1) from error

    additions_count = len(scheduler.tasks) - before_count
    mode_suffix = " (dry-run)" if dry_run else ""
    typer.echo(f"{additions_count} issue(s) discovered and persisted{mode_suffix}")
    for note_issue, note_message in scheduler.discovery_notes:
        typer.echo(f"  #{note_issue}: {note_message}")

    if dry_run:
        return

    claude_sensor = ClaudeCapacitySensor(
        ClaudeExecutionPolicy(
            live_probe_opt_in=True, billing_mode=ClaudeBillingMode.SUBSCRIPTION_VERIFIED
        )
    )
    codex_sensor = CodexCapacitySensor(allow_live=True, subscription_billing_verified=True)
    capacities = (*claude_sensor.observe("claude"), *codex_sensor.observe("codex"))

    try:
        scheduler.run_until_waiting(capacities)
    except KeyboardInterrupt:
        typer.echo("\nInterrupted; in-progress task state was saved safely.", err=True)
        raise typer.Exit(130) from None

    if scheduler.is_waiting_for_capacity:
        wait_seconds = int(scheduler.wait_duration().total_seconds())
        typer.echo(
            f"Waiting for agent capacity; re-run later (earliest retry in ~{wait_seconds}s)"
        )

    counts = Counter(task.status.value for task in scheduler.tasks)
    for name in sorted(counts):
        typer.echo(f"{name:<22} {counts[name]}")


@app.command()
def status(
    ctx: typer.Context,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show detailed per-task info")
    ] = False,
) -> None:
    """Show persisted scheduler state without contacting providers."""
    context: Context = ctx.obj
    try:
        tasks = context.store.load_tasks()
        is_paused = context.store.is_paused()
    except StateCorruptionError as error:
        typer.echo(f"State error: {error}", err=True)
        raise typer.Exit(1) from error
    state = "PAUSED" if is_paused else "READY"
    typer.echo(f"Scheduler {state}")
    if not tasks:
        typer.echo("Queue is empty")
        return
    counts = Counter(task.status.value for task in tasks)
    for name in sorted(counts):
        typer.echo(f"{name:<22} {counts[name]}")

    if verbose:
        typer.echo("\nTasks:")
        for t in tasks:
            pr_str = f" [PR #{t.pr}]" if t.pr else ""
            agent_str = f" (agent: {t.current_agent})" if t.current_agent else ""
            typer.echo(f"  #{t.issue_number:<4} {t.status.value:<18} {t.title}{pr_str}{agent_str}")
            if t.needs_human_reason:
                typer.echo(f"        reason: {t.needs_human_reason}")


@app.command()
def pause(ctx: typer.Context) -> None:
    """Stop new dispatches; an already running worker is not interrupted."""
    context: Context = ctx.obj
    try:
        context.store.set_paused(True)
    except (SchedulerLockError, StateCorruptionError) as error:
        typer.echo(f"State error: {error}", err=True)
        raise typer.Exit(1) from error
    typer.echo("Scheduler paused; running work is unchanged")


@app.command()
def resume(ctx: typer.Context) -> None:
    """Allow new dispatches."""
    context: Context = ctx.obj
    try:
        context.store.set_paused(False)
    except (SchedulerLockError, StateCorruptionError) as error:
        typer.echo(f"State error: {error}", err=True)
        raise typer.Exit(1) from error
    typer.echo("Scheduler resumed")


@app.command()
def cancel(ctx: typer.Context, issue: Annotated[int, typer.Argument(min=1)]) -> None:
    """Cancel one task while preserving its files and handoff."""
    context: Context = ctx.obj
    try:
        with context.store.lock():
            tasks = context.store.load_tasks()
            matches = tuple(task for task in tasks if task.issue_number == issue)
            if not matches:
                typer.echo(f"Issue #{issue} is not in scheduler state", err=True)
                raise typer.Exit(1)
            task = matches[0]
            if task.status is not TaskState.CANCELLED:
                try:
                    replacement = task.transition(TaskState.CANCELLED)
                except ValueError as error:
                    typer.echo(f"Issue #{issue} cannot be cancelled from {task.status}", err=True)
                    raise typer.Exit(1) from error
                updated = tuple(
                    replacement if item.issue_number == issue else item for item in tasks
                )
                context.store.save_tasks(updated, paused=context.store.is_paused())
    except (SchedulerLockError, StateCorruptionError) as error:
        typer.echo(f"State error: {error}", err=True)
        raise typer.Exit(1) from error
    typer.echo(f"Issue #{issue} cancelled; worktree was preserved")


@app.command()
def doctor() -> None:
    """Check required local executables and warn on an overly broad `gh` token scope.

    Reads the locally cached `gh` auth state to report token scopes; never prints the token
    value and never invokes Claude or Codex, so no Agent capacity is consumed.
    """
    commands = ("git", "gh", "claude", "codex")
    missing = tuple(command for command in commands if shutil.which(command) is None)
    for command in commands:
        typer.echo(f"{command:<8} {'FOUND' if command not in missing else 'MISSING'}")
    if "gh" not in missing:
        diagnosis = diagnose_token()
        if diagnosis.authenticated:
            typer.echo(f"gh token scopes: {', '.join(diagnosis.scopes) or '(none)'}")
            if diagnosis.broad_scopes:
                typer.echo(
                    "gh token scope is broader than required for read-only issue discovery: "
                    f"{', '.join(diagnosis.broad_scopes)}"
                )
        else:
            typer.echo("gh token scope could not be determined (not authenticated)")
    if missing:
        raise typer.Exit(1)
    typer.echo("Native workers remain disabled until Phase 2 assumptions are validated")


@app.command()
def metrics(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Output metrics in structured JSON format")
    ] = False,
    report_file: Annotated[
        Path | None, typer.Option("--report", help="Save run report to specified file")
    ] = None,
) -> None:
    """Display Productivity, Reliability, and Capacity metrics."""
    import json

    from subsched.metrics import calculate_metrics, format_run_report

    context: Context = ctx.obj
    try:
        tasks = context.store.load_tasks()
    except StateCorruptionError as error:
        typer.echo(f"State error: {error}", err=True)
        raise typer.Exit(1) from error

    calculated = calculate_metrics(tasks)
    report_text = format_run_report(calculated)

    if report_file is not None:
        try:
            report_file.write_text(report_text, encoding="utf-8")
        except OSError as error:
            typer.echo(f"Failed to write report to {report_file}: {error}", err=True)

    if json_output:
        typer.echo(json.dumps(calculated.to_dict(), indent=2))
    else:
        typer.echo(report_text)
