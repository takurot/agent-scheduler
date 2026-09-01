from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from subsched.config import (
    ConfigError,
    load_config,
    parse_duration,
    parse_natural_language_instruction,
    validate_repo,
)


def test_config_loads_example_yaml() -> None:
    example_path = Path(__file__).parents[2] / "examples" / "scheduler.yaml"
    assert example_path.exists()
    raw = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    assert raw["github"]["completion"]["close_issue"] is False
    assert raw["routing"]["strategy"] == "capacity-aware"
    assert raw["routing"]["provider_capacity"]["preferred"] is True
    assert raw["routing"]["local_estimate"]["proactive_switch"] is False
    assert raw["execution"]["pause_running_policy"] == "continue"
    assert raw["queue"]["priority"]["tie_break"] == "issue_number_asc"
    config = load_config(example_path)

    assert config.github.repo == "owner/project"
    assert config.github.base_branch is None
    assert config.github.include_labels == ("ai-ready",)
    assert config.github.exclude_labels == ("blocked", "human-only", "security-sensitive")
    assert config.execution.concurrency == 1
    assert config.execution.max_agent_switches == 6
    assert config.execution.max_tasks_per_run == 50
    assert config.billing.api_fallback is False
    assert config.billing.metered_usage is False
    assert config.billing.unknown_mode == "disable"
    assert config.github.completion.close_issue is False
    assert config.routing.strategy == "capacity-aware"
    assert config.routing.provider_capacity.preferred is True
    assert config.routing.local_estimate.proactive_switch is False
    assert config.execution.pause_running_policy == "continue"
    assert config.queue.priority.tie_break == "issue_number_asc"


def test_config_loads_all_spec_sections(tmp_path: Path) -> None:
    yaml_content = """
github:
  repo: takurot/project
  base_branch: develop
  mode: label
  include_labels:
    - ai-ready
  exclude_labels:
    - blocked
    - human-only
    - security-sensitive
  completion:
    create_pr: true
    close_issue: false

agents:
  claude:
    enabled: true
    priority: 100
  codex:
    enabled: true
    priority: 90

routing:
  strategy: capacity-aware
  provider_capacity:
    preferred: true
  local_estimate:
    proactive_switch: false

billing:
  api_fallback: false
  metered_usage: false
  unknown_mode: disable

execution:
  concurrency: 1
  max_agent_switches: 6
  max_task_runtime: 6h
  max_tasks_per_run: 50
  pause_running_policy: continue
  agent_timeout_seconds: 450

queue:
  priority:
    label_scores:
      p0: 100
      p1: 50
    tie_break: issue_number_asc

handoff:
  continuous: true

verification:
  commands:
    - pytest
    - ruff check .
"""
    path = tmp_path / "scheduler.yaml"
    path.write_text(yaml_content, encoding="utf-8")
    config = load_config(path)

    assert config.github.repo == "takurot/project"
    assert config.github.base_branch == "develop"
    assert config.agents["claude"].priority == 100
    assert config.agents["codex"].priority == 90
    assert config.routing.strategy == "capacity-aware"
    assert config.execution.pause_running_policy == "continue"
    assert config.execution.agent_timeout_seconds == 450
    assert config.queue.priority.label_scores == {"p0": 100, "p1": 50}
    assert config.queue.priority.tie_break == "issue_number_asc"
    assert config.verification.commands == ("pytest", "ruff check .")
    assert config.verification.timeout_seconds == 120


@pytest.mark.parametrize(
    "base_branch",
    ("--upload-pack=evil", "feature..main", "topic.lock", "bad branch", "@{upstream}"),
)
def test_config_rejects_unsafe_base_branch(tmp_path: Path, base_branch: str) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        f"github:\n  repo: o/r\n  base_branch: {base_branch!r}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"github\.base_branch"):
        load_config(path)


def test_verification_timeout_seconds_default_and_override(tmp_path: Path) -> None:
    default_path = tmp_path / "default.yaml"
    default_path.write_text("github:\n  repo: o/r\n", encoding="utf-8")
    assert load_config(default_path).verification.timeout_seconds == 120

    override_path = tmp_path / "override.yaml"
    override_path.write_text(
        "github:\n  repo: o/r\nverification:\n  timeout_seconds: 300\n",
        encoding="utf-8",
    )
    assert load_config(override_path).verification.timeout_seconds == 300


def test_verification_timeout_seconds_rejects_non_positive(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        "github:\n  repo: o/r\nverification:\n  timeout_seconds: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="positive integer"):
        load_config(path)


@pytest.mark.parametrize(
    "commands_yaml",
    (
        "commands: []",
        "commands:\n    - '   '",
    ),
)
def test_verification_commands_rejects_no_executable_gate(
    tmp_path: Path, commands_yaml: str
) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        f"github:\n  repo: o/r\nverification:\n  {commands_yaml}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="at least one non-blank command"):
        load_config(path)


def test_agent_timeout_seconds_default_and_override(tmp_path: Path) -> None:
    default_path = tmp_path / "default.yaml"
    default_path.write_text("github:\n  repo: o/r\n", encoding="utf-8")
    assert load_config(default_path).execution.agent_timeout_seconds == 300

    override_path = tmp_path / "override.yaml"
    override_path.write_text(
        "github:\n  repo: o/r\nexecution:\n  agent_timeout_seconds: 900\n",
        encoding="utf-8",
    )
    assert load_config(override_path).execution.agent_timeout_seconds == 900


def test_ci_monitoring_defaults_to_disabled(tmp_path: Path) -> None:
    default_path = tmp_path / "default.yaml"
    default_path.write_text("github:\n  repo: o/r\n", encoding="utf-8")
    assert load_config(default_path).execution.ci_monitoring is False

    enabled_path = tmp_path / "enabled.yaml"
    enabled_path.write_text(
        "github:\n  repo: o/r\nexecution:\n  ci_monitoring: true\n", encoding="utf-8"
    )
    assert load_config(enabled_path).execution.ci_monitoring is True


def test_agent_timeout_seconds_rejects_non_positive(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        "github:\n  repo: o/r\nexecution:\n  agent_timeout_seconds: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="positive integer"):
        load_config(path)


def test_config_rejects_parallel_execution_in_phase1(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text("github:\n  repo: o/r\nexecution:\n  concurrency: 2\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="concurrency"):
        load_config(path)


@pytest.mark.parametrize(
    "billing_yaml",
    (
        "api_fallback: true",
        "metered_usage: true",
        "unknown_mode: allow",
    ),
)
def test_config_rejects_unsafe_billing(tmp_path: Path, billing_yaml: str) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        f"github:\n  repo: o/r\nbilling:\n  {billing_yaml}\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="billing must remain fail-closed"):
        load_config(path)


def test_config_rejects_close_issue_true(tmp_path: Path) -> None:
    """Regression test for #136: close_issue: true has no implemented runtime path (it
    would require a separate write-permission tier per SPEC), so accepting it silently
    would be misleading -- config must fail closed instead of just ignoring it."""
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        "github:\n  repo: o/r\n  completion:\n    close_issue: true\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="close_issue"):
        load_config(path)


def test_config_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text("github:\n  repo: o/r\nunknown_key: true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="unknown config keys"):
        load_config(path)


def test_config_strict_validation(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        "github:\n  repo: o/r\nexecution:\n  max_agent_switches: \"six\"\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="positive integer"):
        load_config(path)


def test_config_list_mode_carries_issue_numbers(tmp_path: Path) -> None:
    """Regression test for #144: github.mode: list previously had no field to actually
    carry the Issue numbers, so a config file alone could never define a reproducible
    explicit-Issue run."""
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        "github:\n  repo: o/r\n  mode: list\n  issues:\n    - 103\n    - 101\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.github.mode == "list"
    assert config.github.issues == (103, 101)


def test_config_list_mode_requires_non_empty_issues(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text("github:\n  repo: o/r\n  mode: list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"non-empty github\.issues"):
        load_config(path)


def test_config_list_mode_rejects_empty_issues_list(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        "github:\n  repo: o/r\n  mode: list\n  issues: []\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match=r"non-empty github\.issues"):
        load_config(path)


def test_config_rejects_duplicate_issue_numbers(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        "github:\n  repo: o/r\n  mode: list\n  issues:\n    - 101\n    - 101\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate issue numbers"):
        load_config(path)


def test_config_rejects_non_positive_issue_numbers(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        "github:\n  repo: o/r\n  mode: list\n  issues:\n    - 0\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="positive integer"):
        load_config(path)


def test_config_rejects_issues_list_with_non_list_mode(tmp_path: Path) -> None:
    """Fail-fast on mismatched intent: github.issues set but mode isn't 'list' would
    otherwise be silently ignored, masking an operator's actual intent."""
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        "github:\n  repo: o/r\n  mode: label\n  issues:\n    - 101\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match=r"only used when github\.mode is 'list'"):
        load_config(path)


def test_config_rejects_unimplemented_routing_strategy(tmp_path: Path) -> None:
    """Regression test for #138: routing.strategy had no runtime consumer besides the
    fixed capacity-aware Router implementation -- accepting an arbitrary string here
    would silently pretend a different strategy took effect when it never does."""
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        "github:\n  repo: o/r\nrouting:\n  strategy: round-robin\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError, match=r"routing\.strategy"):
        load_config(path)


def test_config_rejects_unimplemented_provider_capacity_preferred_false(
    tmp_path: Path,
) -> None:
    """Router.select() always prefers fresh provider capacity data unconditionally --
    there is no runtime code path that honors preferred=False, so it must be rejected
    rather than silently ignored."""
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        "github:\n  repo: o/r\nrouting:\n  provider_capacity:\n    preferred: false\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"routing\.provider_capacity\.preferred"):
        load_config(path)


def test_config_rejects_unimplemented_local_estimate_proactive_switch(
    tmp_path: Path,
) -> None:
    """No runtime code implements local-usage-estimate-driven proactive agent
    switching -- proactive_switch=true must fail-fast, not silently no-op."""
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        "github:\n  repo: o/r\nrouting:\n  local_estimate:\n    proactive_switch: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"routing\.local_estimate\.proactive_switch"):
        load_config(path)


def test_config_default_routing_values_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text("github:\n  repo: o/r\n", encoding="utf-8")

    config = load_config(path)
    assert config.routing.strategy == "capacity-aware"
    assert config.routing.provider_capacity.preferred is True
    assert config.routing.local_estimate.proactive_switch is False


def test_config_rejects_unimplemented_queue_tie_break(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        "github:\n  repo: o/r\nqueue:\n  priority:\n    tie_break: fifo\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"queue\.priority\.tie_break"):
        load_config(path)


def test_parse_duration() -> None:
    assert parse_duration("6h") == 21600
    assert parse_duration("30m") == 1800
    assert parse_duration("45s") == 45
    assert parse_duration("1d") == 86400
    assert parse_duration(100) == 100

    with pytest.raises(ConfigError, match="invalid duration format"):
        parse_duration("invalid")


@pytest.mark.parametrize(
    "repo",
    [
        "owner/..",
        "../repo",
        "../..",
        "./repo",
        "owner/.",
        "owner/...",
    ],
)
def test_validate_repo_rejects_dot_only_segments(repo: str) -> None:
    with pytest.raises(ConfigError, match="owner/name format"):
        validate_repo(repo)


@pytest.mark.parametrize(
    "repo",
    [
        "owner/repo",
        "takurot/agent-scheduler",
        "my.org/my.repo",
        "..foo/repo",
        "owner/repo..bak",
    ],
)
def test_validate_repo_accepts_legitimate_values(repo: str) -> None:
    assert validate_repo(repo) == repo


def test_parse_natural_language_instruction() -> None:
    intent1 = parse_natural_language_instruction("GitHubのopen issueをすべて実行")
    assert intent1.issues == "all-open"

    intent2 = parse_natural_language_instruction("owner/projectのopen issueをすべて実行")
    assert intent2.repo == "owner/project"
    assert intent2.issues == "all-open"

    intent3 = parse_natural_language_instruction("issue #101, #103を実行")
    assert intent3.issues == "101,103"


@pytest.mark.parametrize("policy", ["abort", "cancel"])
def test_pause_running_policy_rejects_unimplemented_values(
    tmp_path: Path, policy: str
) -> None:
    """Regression test for #137: abort/cancel currently have no runtime implementation
    (no process-group control, no Task state transition). Accepting them and silently
    doing nothing would be a fail-open safety config -- config load must reject them
    until they are actually implemented."""
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        f"github:\n  repo: o/r\nexecution:\n  pause_running_policy: {policy}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="not supported"):
        load_config(path)


def test_pause_running_policy_continue_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        "github:\n  repo: o/r\nexecution:\n  pause_running_policy: continue\n",
        encoding="utf-8",
    )
    assert load_config(path).execution.pause_running_policy == "continue"
