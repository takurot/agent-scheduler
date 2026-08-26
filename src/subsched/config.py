from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


def parse_duration(value: str | int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        if value <= 0:
            raise ConfigError("duration must be a positive integer")
        return value
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("duration must be a non-empty string or integer")
    match = re.fullmatch(r"(\d+)([smhd])", value.strip())
    if not match:
        raise ConfigError(f"invalid duration format: {value}")
    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0:
        raise ConfigError("duration amount must be positive")
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return amount * multipliers[unit]


def _strict_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{name} must be a boolean")


def _strict_pos_int(value: Any, name: str, *, min_val: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < min_val:
        raise ConfigError(f"{name} must be a positive integer (>= {min_val})")
    return int(value)


def validate_repo(repo: str) -> str:
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ConfigError("repo must use owner/name format")
    owner, name = repo.split("/", 1)
    # Reject segments made up entirely of dots (".", "..", "..." etc.) so a value like
    # "../.." cannot masquerade as an owner/repo pair; a normal owner/repo segment is never
    # only dots.
    if owner.strip(".") == "" or name.strip(".") == "":
        raise ConfigError("repo must use owner/name format")
    return repo


@dataclass(frozen=True, slots=True)
class GitHubCompletionConfig:
    create_pr: bool = True
    close_issue: bool = False


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    repo: str | None = None
    mode: str = "label"
    include_labels: tuple[str, ...] = ("ai-ready",)
    exclude_labels: tuple[str, ...] = ("blocked", "human-only", "security-sensitive")
    completion: GitHubCompletionConfig = GitHubCompletionConfig()


@dataclass(frozen=True, slots=True)
class AgentSettings:
    enabled: bool = True
    priority: int = 100


@dataclass(frozen=True, slots=True)
class ProviderCapacityConfig:
    preferred: bool = True


@dataclass(frozen=True, slots=True)
class LocalEstimateConfig:
    proactive_switch: bool = False


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    strategy: str = "capacity-aware"
    provider_capacity: ProviderCapacityConfig = ProviderCapacityConfig()
    local_estimate: LocalEstimateConfig = LocalEstimateConfig()


@dataclass(frozen=True, slots=True)
class BillingConfig:
    api_fallback: bool = False
    metered_usage: bool = False
    unknown_mode: str = "disable"


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    concurrency: int = 1
    max_agent_switches: int = 6
    max_agent_failures: int = 2
    max_task_runtime: str = "6h"
    max_tasks_per_run: int = 50
    pause_running_policy: str = "continue"
    agent_timeout_seconds: int = 300


@dataclass(frozen=True, slots=True)
class QueuePriorityConfig:
    label_scores: dict[str, int] = field(default_factory=dict)
    tie_break: str = "issue_number_asc"


@dataclass(frozen=True, slots=True)
class QueueConfig:
    priority: QueuePriorityConfig = QueuePriorityConfig()


@dataclass(frozen=True, slots=True)
class HandoffConfig:
    continuous: bool = True


@dataclass(frozen=True, slots=True)
class VerificationConfig:
    commands: tuple[str, ...] = ("pytest", "ruff check .")
    timeout_seconds: int = 120


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    github: GitHubConfig = GitHubConfig()
    agents: dict[str, AgentSettings] = field(
        default_factory=lambda: {
            "claude": AgentSettings(enabled=True, priority=100),
            "codex": AgentSettings(enabled=True, priority=90),
        }
    )
    routing: RoutingConfig = RoutingConfig()
    billing: BillingConfig = BillingConfig()
    execution: ExecutionConfig = ExecutionConfig()
    queue: QueueConfig = QueueConfig()
    handoff: HandoffConfig = HandoffConfig()
    verification: VerificationConfig = VerificationConfig()


ROOT_KEYS = frozenset(
    {
        "github",
        "agents",
        "routing",
        "billing",
        "execution",
        "queue",
        "handoff",
        "verification",
    }
)

SECTION_KEYS: dict[str, frozenset[str]] = {
    "github": frozenset({"repo", "mode", "include_labels", "exclude_labels", "completion"}),
    "routing": frozenset({"strategy", "provider_capacity", "local_estimate"}),
    "billing": frozenset({"api_fallback", "metered_usage", "unknown_mode"}),
    "execution": frozenset(
        {
            "concurrency",
            "max_agent_switches",
            "max_agent_failures",
            "max_task_runtime",
            "max_tasks_per_run",
            "pause_running_policy",
            "agent_timeout_seconds",
        }
    ),
    "queue": frozenset({"priority"}),
    "handoff": frozenset({"continuous"}),
    "verification": frozenset({"commands", "timeout_seconds"}),
}


def _parse_github_config(raw: Mapping[str, Any]) -> GitHubConfig:
    completion_raw = raw.get("completion", {})
    if not isinstance(completion_raw, dict):
        raise ConfigError("github.completion must be a mapping")
    completion_extras = set(completion_raw) - {"create_pr", "close_issue"}
    if completion_extras:
        raise ConfigError(f"unknown github.completion keys: {join_keys(completion_extras)}")

    completion = GitHubCompletionConfig(
        create_pr=_strict_bool(
            completion_raw.get("create_pr", True), "github.completion.create_pr"
        ),
        close_issue=_strict_bool(
            completion_raw.get("close_issue", False), "github.completion.close_issue"
        ),
    )

    repo_val = raw.get("repo")
    repo = validate_repo(repo_val) if repo_val is not None else None
    mode = str(raw.get("mode", "label"))
    if mode not in {"label", "all-open", "list"}:
        raise ConfigError(f"invalid github.mode: {mode}")

    inc_labels = raw.get("include_labels", ("ai-ready",))
    if not isinstance(inc_labels, (list, tuple)):
        raise ConfigError("github.include_labels must be a list of strings")
    exc_labels = raw.get("exclude_labels", ("blocked", "human-only", "security-sensitive"))
    if not isinstance(exc_labels, (list, tuple)):
        raise ConfigError("github.exclude_labels must be a list of strings")

    return GitHubConfig(
        repo=repo,
        mode=mode,
        include_labels=tuple(str(x) for x in inc_labels),
        exclude_labels=tuple(str(x) for x in exc_labels),
        completion=completion,
    )


def join_keys(keys: set[str]) -> str:
    return ", ".join(sorted(keys))


def _parse_agents_config(raw: Any) -> dict[str, AgentSettings]:
    if not isinstance(raw, dict):
        raise ConfigError("agents must be a mapping")
    agents: dict[str, AgentSettings] = {}
    for name, item in raw.items():
        if not isinstance(item, dict):
            raise ConfigError(f"agents.{name} must be a mapping")
        extras = set(item) - {"enabled", "priority"}
        if extras:
            raise ConfigError(f"unknown agents.{name} keys: {join_keys(extras)}")
        enabled = _strict_bool(item.get("enabled", True), f"agents.{name}.enabled")
        priority = _strict_pos_int(item.get("priority", 100), f"agents.{name}.priority", min_val=0)
        agents[str(name)] = AgentSettings(enabled=enabled, priority=priority)
    return agents


def _parse_routing_config(raw: Mapping[str, Any]) -> RoutingConfig:
    strategy = str(raw.get("strategy", "capacity-aware"))
    pc_raw = raw.get("provider_capacity", {})
    if not isinstance(pc_raw, dict):
        raise ConfigError("routing.provider_capacity must be a mapping")
    pc_extras = set(pc_raw) - {"preferred"}
    if pc_extras:
        raise ConfigError(f"unknown routing.provider_capacity keys: {join_keys(pc_extras)}")
    provider_capacity = ProviderCapacityConfig(
        preferred=_strict_bool(
            pc_raw.get("preferred", True), "routing.provider_capacity.preferred"
        )
    )

    le_raw = raw.get("local_estimate", {})
    if not isinstance(le_raw, dict):
        raise ConfigError("routing.local_estimate must be a mapping")
    le_extras = set(le_raw) - {"proactive_switch"}
    if le_extras:
        raise ConfigError(f"unknown routing.local_estimate keys: {join_keys(le_extras)}")
    local_estimate = LocalEstimateConfig(
        proactive_switch=_strict_bool(
            le_raw.get("proactive_switch", False), "routing.local_estimate.proactive_switch"
        )
    )
    return RoutingConfig(
        strategy=strategy,
        provider_capacity=provider_capacity,
        local_estimate=local_estimate,
    )


def _parse_billing_config(raw: Mapping[str, Any]) -> BillingConfig:
    api_fallback = _strict_bool(raw.get("api_fallback", False), "billing.api_fallback")
    metered_usage = _strict_bool(raw.get("metered_usage", False), "billing.metered_usage")
    unknown_mode = str(raw.get("unknown_mode", "disable"))
    if api_fallback or metered_usage or unknown_mode != "disable":
        raise ConfigError("billing must remain fail-closed in Phase 1")
    return BillingConfig(
        api_fallback=api_fallback,
        metered_usage=metered_usage,
        unknown_mode=unknown_mode,
    )


def _parse_execution_config(raw: Mapping[str, Any]) -> ExecutionConfig:
    concurrency = _strict_pos_int(raw.get("concurrency", 1), "execution.concurrency")
    if concurrency != 1:
        raise ConfigError("Phase 1 supports execution.concurrency = 1 only")
    max_switches = _strict_pos_int(
        raw.get("max_agent_switches", 6), "execution.max_agent_switches"
    )
    max_failures = _strict_pos_int(
        raw.get("max_agent_failures", 2), "execution.max_agent_failures"
    )
    max_tasks = _strict_pos_int(raw.get("max_tasks_per_run", 50), "execution.max_tasks_per_run")
    runtime = raw.get("max_task_runtime", "6h")
    parse_duration(runtime)
    policy = str(raw.get("pause_running_policy", "continue"))
    if policy not in {"continue", "abort", "cancel"}:
        raise ConfigError(f"invalid execution.pause_running_policy: {policy}")
    agent_timeout_seconds = _strict_pos_int(
        raw.get("agent_timeout_seconds", 300), "execution.agent_timeout_seconds"
    )
    return ExecutionConfig(
        concurrency=concurrency,
        max_agent_switches=max_switches,
        max_agent_failures=max_failures,
        max_task_runtime=str(runtime),
        max_tasks_per_run=max_tasks,
        pause_running_policy=policy,
        agent_timeout_seconds=agent_timeout_seconds,
    )


def _parse_queue_config(raw: Mapping[str, Any]) -> QueueConfig:
    priority_raw = raw.get("priority", {})
    if not isinstance(priority_raw, dict):
        raise ConfigError("queue.priority must be a mapping")
    extras = set(priority_raw) - {"label_scores", "tie_break"}
    if extras:
        raise ConfigError(f"unknown queue.priority keys: {join_keys(extras)}")
    label_scores_raw = priority_raw.get("label_scores", {})
    if not isinstance(label_scores_raw, dict):
        raise ConfigError("queue.priority.label_scores must be a mapping")
    label_scores = {
        str(k): _strict_pos_int(v, f"label_scores.{k}", min_val=0)
        for k, v in label_scores_raw.items()
    }
    tie_break = str(priority_raw.get("tie_break", "issue_number_asc"))
    if tie_break != "issue_number_asc":
        raise ConfigError(f"unsupported queue.priority.tie_break: {tie_break}")
    return QueueConfig(
        priority=QueuePriorityConfig(label_scores=label_scores, tie_break=tie_break)
    )


def _parse_handoff_config(raw: Mapping[str, Any]) -> HandoffConfig:
    continuous = _strict_bool(raw.get("continuous", True), "handoff.continuous")
    return HandoffConfig(continuous=continuous)


def _parse_verification_config(raw: Mapping[str, Any]) -> VerificationConfig:
    commands_raw = raw.get("commands", ("pytest", "ruff check ."))
    if not isinstance(commands_raw, (list, tuple)):
        raise ConfigError("verification.commands must be a list of strings")
    timeout_seconds = _strict_pos_int(
        raw.get("timeout_seconds", 120), "verification.timeout_seconds"
    )
    return VerificationConfig(
        commands=tuple(str(c) for c in commands_raw),
        timeout_seconds=timeout_seconds,
    )


def load_config(path: Path) -> SchedulerConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"cannot read config: {path}") from error
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    unknown = set(raw) - ROOT_KEYS
    if unknown:
        raise ConfigError(f"unknown config keys: {join_keys(unknown)}")
    for section, allowed in SECTION_KEYS.items():
        if section in raw:
            value = raw[section]
            if not isinstance(value, dict):
                raise ConfigError(f"{section} must be a mapping")
            extras = set(value) - allowed
            if extras:
                raise ConfigError(f"unknown {section} keys: {join_keys(extras)}")

    github = _parse_github_config(raw.get("github", {}))
    agents = (
        _parse_agents_config(raw["agents"])
        if "agents" in raw
        else {
            "claude": AgentSettings(enabled=True, priority=100),
            "codex": AgentSettings(enabled=True, priority=90),
        }
    )
    routing = _parse_routing_config(raw.get("routing", {}))
    billing = _parse_billing_config(raw.get("billing", {}))
    execution = _parse_execution_config(raw.get("execution", {}))
    queue = _parse_queue_config(raw.get("queue", {}))
    handoff = _parse_handoff_config(raw.get("handoff", {}))
    verification = _parse_verification_config(raw.get("verification", {}))

    return SchedulerConfig(
        github=github,
        agents=agents,
        routing=routing,
        billing=billing,
        execution=execution,
        queue=queue,
        handoff=handoff,
        verification=verification,
    )


@dataclass(frozen=True, slots=True)
class NaturalLanguageIntent:
    repo: str | None = None
    issues: str | None = None
    label: str | None = None


def parse_natural_language_instruction(instruction: str) -> NaturalLanguageIntent:
    if not isinstance(instruction, str) or not instruction.strip():
        raise ConfigError("instruction must not be empty")
    text = instruction.strip()
    repo: str | None = None
    repo_match = re.search(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text)
    if repo_match:
        repo = repo_match.group(1)

    issues: str | None = None
    all_open_pattern = (
        r"(?i)(open\s*issue[s]?をすべて|全件|all[\s-]open|all open issues|すべて実行)"
    )
    if re.search(all_open_pattern, text):
        issues = "all-open"
    else:
        issue_nums = re.findall(r"#?(\d+)", text)
        if issue_nums:
            issues = ",".join(issue_nums)

    label: str | None = None
    label_match = re.search(r"--label\s+([A-Za-z0-9_.-]+)|label[:\s]+([A-Za-z0-9_.-]+)", text)
    if label_match:
        label = label_match.group(1) or label_match.group(2)

    return NaturalLanguageIntent(repo=repo, issues=issues, label=label)
