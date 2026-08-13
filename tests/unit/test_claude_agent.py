from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from subsched.agents.claude import (
    ClaudeBillingMode,
    ClaudeCliMetadataError,
    ClaudeExecutionPolicy,
    ClaudeProbeBlocked,
    ClaudeProcessOutcome,
    parse_claude_cli_metadata,
    parse_claude_result,
)
from subsched.models import AgentResultKind, CapacityState

FIXTURES = Path(__file__).parents[1] / "fixtures" / "claude"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["success.json", "structured-success.json"])
def test_parses_headless_success_formats_without_retaining_agent_output(name: str) -> None:
    result = parse_claude_result(ClaudeProcessOutcome(exit_code=0, stdout=fixture(name)))

    assert result.kind is AgentResultKind.PASS
    assert result.output == "claude completed"


@pytest.mark.parametrize(
    ("name", "kind", "reset_at"),
    [
        ("session-capacity.json", AgentResultKind.CAPACITY_SESSION, "2026-08-13T13:00:00+09:00"),
        ("weekly-capacity.json", AgentResultKind.CAPACITY_WEEKLY, "2026-08-17T09:00:00+09:00"),
    ],
)
def test_classifies_capacity_with_structured_reset(
    name: str, kind: AgentResultKind, reset_at: str
) -> None:
    result = parse_claude_result(ClaudeProcessOutcome(exit_code=1, stdout=fixture(name)))

    assert result.kind is kind
    assert result.reset_at == datetime.fromisoformat(reset_at)


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        ("Service temporarily overloaded; please try again", AgentResultKind.CAPACITY_TEMPORARY),
        ("Authentication failed. Run /login.", AgentResultKind.AUTH_ERROR),
        ("Credit balance is too low", AgentResultKind.BILLING_ERROR),
        ("Permission denied for tool Bash", AgentResultKind.PERMISSION_DENIED),
    ],
)
def test_classifies_known_stderr_without_copying_it(stderr: str, kind: AgentResultKind) -> None:
    result = parse_claude_result(ClaudeProcessOutcome(exit_code=1, stdout="", stderr=stderr))

    assert result.kind is kind
    assert stderr not in result.output


def test_classifies_structured_generic_failure() -> None:
    result = parse_claude_result(
        ClaudeProcessOutcome(exit_code=1, stdout=fixture("failure.json"))
    )

    assert result.kind is AgentResultKind.FAILURE


@pytest.mark.parametrize(
    ("outcome", "kind"),
    [
        (ClaudeProcessOutcome(exit_code=-15, stdout="", timed_out=True), AgentResultKind.TIMEOUT),
        (
            ClaudeProcessOutcome(
                exit_code=-9, stdout="", timed_out=True, cleanup_succeeded=False
            ),
            AgentResultKind.PROCESS_CLEANUP_FAILED,
        ),
        (ClaudeProcessOutcome(exit_code=0, stdout='{"type":"future"}'), AgentResultKind.UNKNOWN),
        (ClaudeProcessOutcome(exit_code=1, stdout="not-json"), AgentResultKind.UNKNOWN),
    ],
)
def test_timeout_cleanup_and_unknown_paths_fail_closed(
    outcome: ClaudeProcessOutcome, kind: AgentResultKind
) -> None:
    assert parse_claude_result(outcome).kind is kind


def test_capacity_without_a_valid_reset_fails_closed() -> None:
    output = fixture("session-capacity.json").replace(
        '"2026-08-13T13:00:00+09:00"', '"not-a-timestamp"'
    )

    assert parse_claude_result(ClaudeProcessOutcome(exit_code=1, stdout=output)).kind is (
        AgentResultKind.UNKNOWN
    )


def test_invalid_unicode_from_process_output_fails_closed() -> None:
    outcome = ClaudeProcessOutcome(exit_code=1, stdout='{"result":"\ud800"}')

    assert parse_claude_result(outcome).kind is AgentResultKind.UNKNOWN


def test_replays_sanitized_live_inconsistent_result_fail_closed() -> None:
    result = parse_claude_result(
        ClaudeProcessOutcome(exit_code=1, stdout=fixture("live-inconsistent-result.json"))
    )

    assert result.kind is AgentResultKind.UNKNOWN
    assert result.output == "claude result unknown"


def test_cli_metadata_requires_scheduler_timeout_when_max_turns_is_absent() -> None:
    metadata = parse_claude_cli_metadata(
        version_output=fixture("cli-version.txt"), help_output=fixture("cli-help.txt")
    )

    assert metadata.version == "2.1.229"
    assert metadata.supports_json_output is True
    assert metadata.supports_permission_mode is True
    assert metadata.supports_max_turns is False
    assert metadata.requires_scheduler_timeout is True
    assert metadata.dangerous_permission_bypass_available is True


def test_cli_metadata_rejects_missing_fail_closed_flags() -> None:
    with pytest.raises(ClaudeCliMetadataError, match="required Claude CLI flags"):
        parse_claude_cli_metadata(version_output="2.1.229", help_output="--print")


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (
            ClaudeExecutionPolicy(
                live_probe_opt_in=False,
                billing_mode=ClaudeBillingMode.SUBSCRIPTION_VERIFIED,
            ),
            "opt-in",
        ),
        (
            ClaudeExecutionPolicy(
                live_probe_opt_in=True,
                billing_mode=ClaudeBillingMode.UNKNOWN,
            ),
            "billing",
        ),
        (
            ClaudeExecutionPolicy(
                live_probe_opt_in=True,
                billing_mode=ClaudeBillingMode.METERED,
            ),
            "metered",
        ),
    ],
)
def test_live_probe_policy_fails_closed(policy: ClaudeExecutionPolicy, message: str) -> None:
    with pytest.raises(ClaudeProbeBlocked, match=message):
        policy.require_live_probe()


def test_unknown_billing_disables_worker_and_normalizes_to_agent_result() -> None:
    policy = ClaudeExecutionPolicy(
        live_probe_opt_in=True,
        billing_mode=ClaudeBillingMode.UNKNOWN,
    )

    assert policy.worker_state is CapacityState.DISABLED_BILLING
    assert policy.blocked_result.kind is AgentResultKind.UNKNOWN_BILLING


def test_verified_subscription_and_explicit_opt_in_allow_probe() -> None:
    policy = ClaudeExecutionPolicy(
        live_probe_opt_in=True,
        billing_mode=ClaudeBillingMode.SUBSCRIPTION_VERIFIED,
    )

    policy.require_live_probe()
    assert policy.worker_state is CapacityState.AVAILABLE
