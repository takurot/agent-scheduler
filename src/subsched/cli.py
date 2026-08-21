from __future__ import annotations

import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer

from subsched.config import (
    ConfigError,
    SchedulerConfig,
    load_config,
    parse_natural_language_instruction,
    validate_repo,
)
from subsched.github.issues import GitHubCliError, GitHubIssueSource, diagnose_token
from subsched.models import Task, TaskState
from subsched.storage import JsonStateStore, SchedulerLockError, StateCorruptionError

app = typer.Typer(no_args_is_help=True, help="Subscription-aware coding agent scheduler")
RepositoryOption = Annotated[
    Path,
    typer.Option("--repository", hidden=True, file_okay=False, resolve_path=True),
]


class Context:
    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self.store = JsonStateStore(repository)


@app.callback()
def main(ctx: typer.Context, repository: RepositoryOption = Path(".")) -> None:
    ctx.obj = Context(repository)


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

    requested: frozenset[int] | None = None
    if resolved_issues is not None and resolved_issues != "all-open":
        requested = frozenset(_parse_issue_numbers(resolved_issues))
    try:
        open_issues = GitHubIssueSource().list_open(resolved_repo, label=resolved_label)
    except GitHubCliError as error:
        typer.echo("GitHub discovery failed (gh exit/error); verify authentication", err=True)
        raise typer.Exit(1) from error

    exclude_labels = frozenset(cfg.github.exclude_labels)
    discovered = tuple(
        issue
        for issue in open_issues
        if (requested is None or issue.number in requested)
        and not exclude_labels.intersection(issue.labels)
    )
    if requested is not None:
        missing = requested - {issue.number for issue in open_issues}
        if missing:
            typer.echo("Requested issues are not open or were not found", err=True)
            raise typer.Exit(1)

    try:
        with context.store.lock():
            persisted = context.store.load_tasks()
            existing = {task.issue_number for task in persisted}
            additions = tuple(task for task in discovered if task.number not in existing)
            max_tasks = cfg.execution.max_tasks_per_run
            if len(persisted) + len(additions) > max_tasks:
                typer.echo(f"Task limit exceeded ({max_tasks})", err=True)
                raise typer.Exit(2)
            tasks = (*persisted, *(Task.from_issue(item) for item in additions))
            context.store.save_tasks(tasks, paused=context.store.is_paused())
    except SchedulerLockError as error:
        typer.echo(f"Lock error: {error}", err=True)
        raise typer.Exit(1) from error
    typer.echo(f"{len(additions)} issue(s) discovered and persisted (dry-run)")


@app.command()
def status(ctx: typer.Context) -> None:
    """Show persisted scheduler state without contacting providers."""
    context: Context = ctx.obj
    try:
        tasks = context.store.load_tasks()
    except StateCorruptionError as error:
        typer.echo(f"State error: {error}", err=True)
        raise typer.Exit(1) from error
    state = "PAUSED" if context.store.is_paused() else "READY"
    typer.echo(f"Scheduler {state}")
    if not tasks:
        typer.echo("Queue is empty")
        return
    counts = Counter(task.status.value for task in tasks)
    for name in sorted(counts):
        typer.echo(f"{name:<22} {counts[name]}")


@app.command()
def pause(ctx: typer.Context) -> None:
    """Stop new dispatches; an already running worker is not interrupted."""
    context: Context = ctx.obj
    try:
        context.store.set_paused(True)
    except SchedulerLockError as error:
        typer.echo(f"Lock error: {error}", err=True)
        raise typer.Exit(1) from error
    typer.echo("Scheduler paused; running work is unchanged")


@app.command()
def resume(ctx: typer.Context) -> None:
    """Allow new dispatches."""
    context: Context = ctx.obj
    try:
        context.store.set_paused(False)
    except SchedulerLockError as error:
        typer.echo(f"Lock error: {error}", err=True)
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
    except SchedulerLockError as error:
        typer.echo(f"Lock error: {error}", err=True)
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
