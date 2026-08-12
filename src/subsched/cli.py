from __future__ import annotations

import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer

from subsched.config import ConfigError, validate_repo
from subsched.github.issues import GitHubCliError, GitHubIssueSource
from subsched.models import Task, TaskState
from subsched.storage import JsonStateStore, StateCorruptionError

app = typer.Typer(no_args_is_help=True, help="Subscription-aware coding agent scheduler")
DEFAULT_EXCLUDE_LABELS = frozenset({"blocked", "human-only", "security-sensitive"})
MAX_TASKS_PER_RUN = 50
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
    repo: Annotated[str, typer.Option("--repo", help="GitHub owner/repository")],
    label: Annotated[str | None, typer.Option("--label")] = None,
    issues: Annotated[str | None, typer.Option("--issues")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Discover and persist only; do not invoke workers")
    ] = False,
) -> None:
    """Discover issues and initialize the durable queue."""
    context: Context = ctx.obj
    try:
        validate_repo(repo)
    except ConfigError as error:
        raise typer.BadParameter(str(error), param_hint="--repo") from error
    if (label is None) == (issues is None):
        raise typer.BadParameter("select exactly one of --label or --issues")
    if not dry_run:
        typer.echo("native workers are not enabled in Phase 1; rerun with --dry-run", err=True)
        raise typer.Exit(2)

    requested: frozenset[int] | None = None
    if issues is not None and issues != "all-open":
        requested = frozenset(_parse_issue_numbers(issues))
    try:
        open_issues = GitHubIssueSource().list_open(repo, label=label)
    except GitHubCliError as error:
        typer.echo("GitHub discovery failed (gh exit/error); verify authentication", err=True)
        raise typer.Exit(1) from error
    discovered = tuple(
        issue
        for issue in open_issues
        if (requested is None or issue.number in requested)
        and not DEFAULT_EXCLUDE_LABELS.intersection(issue.labels)
    )
    if requested is not None:
        missing = requested - {issue.number for issue in open_issues}
        if missing:
            typer.echo("Requested issues are not open or were not found", err=True)
            raise typer.Exit(1)

    persisted = context.store.load_tasks()
    existing = {task.issue_number for task in persisted}
    additions = tuple(task for task in discovered if task.number not in existing)
    if len(persisted) + len(additions) > MAX_TASKS_PER_RUN:
        typer.echo(f"Task limit exceeded ({MAX_TASKS_PER_RUN})", err=True)
        raise typer.Exit(2)
    tasks = (*persisted, *(Task.from_issue(item) for item in additions))
    context.store.save_tasks(tasks, paused=context.store.is_paused())
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
    context.store.set_paused(True)
    typer.echo("Scheduler paused; running work is unchanged")


@app.command()
def resume(ctx: typer.Context) -> None:
    """Allow new dispatches."""
    context: Context = ctx.obj
    context.store.set_paused(False)
    typer.echo("Scheduler resumed")


@app.command()
def cancel(ctx: typer.Context, issue: Annotated[int, typer.Argument(min=1)]) -> None:
    """Cancel one task while preserving its files and handoff."""
    context: Context = ctx.obj
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
        updated = tuple(replacement if item.issue_number == issue else item for item in tasks)
        context.store.save_tasks(updated, paused=context.store.is_paused())
    typer.echo(f"Issue #{issue} cancelled; worktree was preserved")


@app.command()
def doctor() -> None:
    """Check required local executables without using credentials or capacity."""
    commands = ("git", "gh", "claude", "codex")
    missing = tuple(command for command in commands if shutil.which(command) is None)
    for command in commands:
        typer.echo(f"{command:<8} {'FOUND' if command not in missing else 'MISSING'}")
    if missing:
        raise typer.Exit(1)
    typer.echo("Native workers remain disabled until Phase 2 assumptions are validated")
