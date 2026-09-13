"""Shared CLI/MCP issue intent, selection, and label eligibility."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

import typer

from subsched.config import (
    ConfigError,
    SchedulerConfig,
    parse_natural_language_instruction,
    validate_repo,
)
from subsched.github.issues import GitHubCliError, GitHubIssueSource
from subsched.models import Issue


def parse_issue_numbers(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(,\d+)*", value):
        raise typer.BadParameter("issues must be all-open or comma-separated positive integers")
    numbers = tuple(int(item) for item in value.split(","))
    if any(number <= 0 for number in numbers) or len(set(numbers)) != len(numbers):
        raise typer.BadParameter("issue numbers must be positive and unique")
    return numbers


@dataclass(frozen=True)
class ResolvedIntent:
    """Effective selection shared by CLI run/config validation and MCP queueing."""

    cfg: SchedulerConfig
    repo: str
    label: str | None
    # "all-open", a comma-separated issue-number string, or None (only when label is set).
    issues: str | None
    labels: tuple[str, ...] = ()


def resolve_intent(
    *,
    cfg: SchedulerConfig,
    query: str | None,
    repo: str | None,
    label: str | None,
    issues: str | None,
) -> ResolvedIntent:
    resolved_repo = repo
    resolved_label = label if issues is None else None
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

            parsed_parts: list[str] = []
            if intent.repo:
                parsed_parts.append(f"repo='{intent.repo}'")
            if intent.label:
                parsed_parts.append(f"label='{intent.label}'")
            if intent.issues:
                parsed_parts.append(f"issues='{intent.issues}'")
            if parsed_parts:
                typer.echo(f"Parsed intent: {', '.join(parsed_parts)}")
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

    resolved_labels: tuple[str, ...] = ()
    if resolved_label is not None:
        resolved_labels = tuple(item.strip() for item in resolved_label.split(",") if item.strip())
        if not resolved_labels:
            raise typer.BadParameter("label must not be empty")

    if resolved_label is None and resolved_issues is None:
        # CLI --label/--issues (and natural-language query) are already applied above and
        # take precedence over config -- this branch only runs when neither was given, so
        # config.github.mode is consulted as the fallback, and a safe default error last.
        if cfg.github.mode == "all-open":
            resolved_issues = "all-open"
        elif cfg.github.mode == "list" and cfg.github.issues:
            resolved_issues = ",".join(str(n) for n in cfg.github.issues)
        elif cfg.github.mode == "label" and cfg.github.include_labels:
            resolved_labels = tuple(cfg.github.include_labels)
            resolved_label = ", ".join(resolved_labels)
        else:
            raise typer.BadParameter("select exactly one of --label or --issues")

    # #144 code review: validate --issues syntax here (not only inside run(), after its
    # native-opt-in gate) so config validate actually validates it too, instead of
    # reporting "Configuration is valid." for a value run() would reject.
    if resolved_issues is not None and resolved_issues != "all-open":
        parse_issue_numbers(resolved_issues)

    return ResolvedIntent(
        cfg=cfg,
        repo=resolved_repo,
        label=resolved_label,
        issues=resolved_issues,
        labels=resolved_labels,
    )


def excluded_issue_labels(
    issue: Issue,
    exclude_labels: frozenset[str],
) -> frozenset[str]:
    """Security-sensitive is always excluded, including with an empty config list."""
    return (exclude_labels | {"security-sensitive"}).intersection(issue.labels)


def select_issues(intent: ResolvedIntent, open_issues: Iterable[Issue]) -> tuple[Issue, ...]:
    snapshot = tuple(open_issues)
    requested = (
        frozenset(parse_issue_numbers(intent.issues))
        if intent.issues is not None and intent.issues != "all-open"
        else None
    )
    if requested is not None and requested - {issue.number for issue in snapshot}:
        raise ValueError("Requested issues are not open or were not found")
    return tuple(
        issue
        for issue in snapshot
        if (requested is None or issue.number in requested)
        and all(label in issue.labels for label in intent.labels)
    )


def discover_selected_issues(
    intent: ResolvedIntent,
    source: GitHubIssueSource,
) -> tuple[Issue, ...]:
    if len(intent.labels) > 1:
        snapshot = source.list_open(intent.repo, labels=intent.labels)
    elif intent.labels:
        snapshot = source.list_open(intent.repo, label=intent.labels[0])
    else:
        snapshot = source.list_open(intent.repo)
    try:
        return select_issues(intent, snapshot)
    except ValueError as error:
        raise GitHubCliError(str(error)) from error
