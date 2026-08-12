from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    repo: str
    include_labels: tuple[str, ...] = ("ai-ready",)
    exclude_labels: tuple[str, ...] = ("blocked", "human-only", "security-sensitive")


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    concurrency: int = 1
    max_agent_switches: int = 6
    max_tasks_per_run: int = 50


@dataclass(frozen=True, slots=True)
class BillingConfig:
    api_fallback: bool = False
    metered_usage: bool = False
    unknown_mode: str = "disable"


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    github: GitHubConfig
    execution: ExecutionConfig = ExecutionConfig()
    billing: BillingConfig = BillingConfig()


ROOT_KEYS = frozenset({"github", "execution", "billing"})
SECTION_KEYS = {
    "github": frozenset({"repo", "include_labels", "exclude_labels", "completion", "mode"}),
    "execution": frozenset(
        {"concurrency", "max_agent_switches", "max_tasks_per_run", "max_task_runtime"}
    ),
    "billing": frozenset({"api_fallback", "metered_usage", "unknown_mode"}),
}


def validate_repo(repo: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ConfigError("repo must use owner/name format")
    return repo


def load_config(path: Path) -> SchedulerConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"cannot read config: {path}") from error
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    unknown = set(raw) - ROOT_KEYS
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")
    for section, allowed in SECTION_KEYS.items():
        value = raw.get(section, {})
        if not isinstance(value, dict):
            raise ConfigError(f"{section} must be a mapping")
        extras = set(value) - allowed
        if extras:
            raise ConfigError(f"unknown {section} keys: {', '.join(sorted(extras))}")

    github_raw: dict[str, Any] = raw.get("github", {})
    execution_raw: dict[str, Any] = raw.get("execution", {})
    billing_raw: dict[str, Any] = raw.get("billing", {})
    try:
        github = GitHubConfig(
            repo=validate_repo(str(github_raw["repo"])),
            include_labels=tuple(github_raw.get("include_labels", ("ai-ready",))),
            exclude_labels=tuple(
                github_raw.get("exclude_labels", ("blocked", "human-only", "security-sensitive"))
            ),
        )
        execution = ExecutionConfig(
            concurrency=int(execution_raw.get("concurrency", 1)),
            max_agent_switches=int(execution_raw.get("max_agent_switches", 6)),
            max_tasks_per_run=int(execution_raw.get("max_tasks_per_run", 50)),
        )
        billing = BillingConfig(
            api_fallback=bool(billing_raw.get("api_fallback", False)),
            metered_usage=bool(billing_raw.get("metered_usage", False)),
            unknown_mode=str(billing_raw.get("unknown_mode", "disable")),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ConfigError("invalid scheduler configuration") from error
    if execution.concurrency != 1:
        raise ConfigError("Phase 1 supports execution.concurrency = 1 only")
    if billing.api_fallback or billing.metered_usage or billing.unknown_mode != "disable":
        raise ConfigError("billing must remain fail-closed in Phase 1")
    return SchedulerConfig(github=github, execution=execution, billing=billing)
