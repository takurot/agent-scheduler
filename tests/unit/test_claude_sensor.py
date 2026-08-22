from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.agents.claude import ClaudeBillingMode, ClaudeExecutionPolicy, ClaudeProcessOutcome
from subsched.capacity.base import CapacitySensor
from subsched.capacity.claude import ClaudeCapacitySensor, parse_claude_capacity
from subsched.models import CapacityState


def test_parse_claude_provider_rate_limits_dual_windows() -> None:
    now = datetime.now(UTC)
    t_5h = now + timedelta(hours=2)
    t_7d = now + timedelta(days=4)
    payload = {
        "rate_limits": {
            "five_hour": {
                "used_percentage": 45.0,
                "resets_at": int(t_5h.timestamp()),
            },
            "seven_day": {
                "used_percentage": 85.0,
                "resets_at": int(t_7d.timestamp()),
            },
        }
    }

    capacities = parse_claude_capacity(json.dumps(payload), observed_at=now)
    assert len(capacities) == 2

    by_scope = {c.scope: c for c in capacities}
    assert "five_hour" in by_scope
    assert "seven_day" in by_scope

    cap_5h = by_scope["five_hour"]
    assert cap_5h.agent == "claude"
    assert cap_5h.state == CapacityState.AVAILABLE
    assert cap_5h.used_percentage == 45.0
    assert cap_5h.source == "provider"
    assert cap_5h.confidence == "high"
    assert cap_5h.reset_at is not None
    assert abs((cap_5h.reset_at - t_5h).total_seconds()) < 2

    cap_7d = by_scope["seven_day"]
    assert cap_7d.agent == "claude"
    assert cap_7d.state == CapacityState.PRESSURED_WEEKLY
    assert cap_7d.used_percentage == 85.0
    assert cap_7d.source == "provider"


def test_parse_claude_provider_rate_limits_iso_timestamps() -> None:
    now = datetime.now(UTC)
    payload = {
        "rate_limits": {
            "five_hour": {
                "used_percentage": 100.0,
                "resets_at": "2026-08-13T13:00:00+00:00",
            },
            "seven_day": {
                "used_percentage": 20.0,
                "resets_at": "2026-08-17T09:00:00+00:00",
            },
        }
    }

    capacities = parse_claude_capacity(payload, observed_at=now)
    by_scope = {c.scope: c for c in capacities}
    assert by_scope["five_hour"].state == CapacityState.COOLDOWN_SESSION
    assert by_scope["seven_day"].state == CapacityState.AVAILABLE


def test_parse_claude_session_capacity_fixture() -> None:
    fixture_path = Path("tests/fixtures/claude/session-capacity.json")
    payload = fixture_path.read_text(encoding="utf-8")
    now = datetime.now(UTC)

    capacities = parse_claude_capacity(payload, observed_at=now)
    assert len(capacities) == 1
    cap = capacities[0]
    assert cap.agent == "claude"
    assert cap.state == CapacityState.COOLDOWN_SESSION
    assert cap.scope == "five_hour"
    assert cap.source == "structured_result"
    assert cap.confidence == "high"
    assert cap.reset_at == datetime.fromisoformat("2026-08-13T13:00:00+09:00")


def test_parse_claude_weekly_capacity_fixture() -> None:
    fixture_path = Path("tests/fixtures/claude/weekly-capacity.json")
    payload = fixture_path.read_text(encoding="utf-8")
    now = datetime.now(UTC)

    capacities = parse_claude_capacity(payload, observed_at=now)
    assert len(capacities) == 1
    cap = capacities[0]
    assert cap.agent == "claude"
    assert cap.state == CapacityState.COOLDOWN_WEEKLY
    assert cap.scope == "seven_day"
    assert cap.source == "structured_result"
    assert cap.confidence == "high"
    assert cap.reset_at == datetime.fromisoformat("2026-08-17T09:00:00+09:00")


def test_parse_claude_auth_and_billing_errors() -> None:
    now = datetime.now(UTC)
    auth_outcome = ClaudeProcessOutcome(
        exit_code=1,
        stdout="",
        stderr="Authentication failed. Run /login to authenticate.",
    )
    auth_caps = parse_claude_capacity(auth_outcome, observed_at=now)
    assert len(auth_caps) == 1
    assert auth_caps[0].state == CapacityState.AUTH_ERROR

    billing_outcome = ClaudeProcessOutcome(
        exit_code=1,
        stdout="",
        stderr="Credit balance too low. Billing rejected.",
    )
    billing_caps = parse_claude_capacity(billing_outcome, observed_at=now)
    assert len(billing_caps) == 1
    assert billing_caps[0].state == CapacityState.DISABLED_BILLING


def test_parse_claude_temporary_capacity_and_pass() -> None:
    now = datetime.now(UTC)
    temp_outcome = ClaudeProcessOutcome(
        exit_code=1,
        stdout="",
        stderr="Claude is overloaded. Please try again later.",
    )
    caps = parse_claude_capacity(temp_outcome, observed_at=now)
    assert len(caps) == 1
    assert caps[0].state == CapacityState.RATE_LIMITED_TEMPORARY
    assert caps[0].scope == "temporary"

    pass_outcome = ClaudeProcessOutcome(
        exit_code=0,
        stdout=json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 1,
                "result": "success",
            }
        ),
        stderr="",
    )
    pass_caps = parse_claude_capacity(pass_outcome, observed_at=now)
    assert len(pass_caps) == 1
    assert pass_caps[0].state == CapacityState.AVAILABLE


def test_parse_claude_schema_drift_classifies_as_unknown() -> None:
    now = datetime.now(UTC)

    # Inconsistent fixture
    inconsistent = Path("tests/fixtures/claude/live-inconsistent-result.json").read_text(
        encoding="utf-8"
    )
    caps = parse_claude_capacity(inconsistent, observed_at=now)
    assert len(caps) == 1
    assert caps[0].state == CapacityState.UNKNOWN

    # Malformed json
    bad_json_caps = parse_claude_capacity("not valid json", observed_at=now)
    assert len(bad_json_caps) == 1
    assert bad_json_caps[0].state == CapacityState.UNKNOWN

    # Unexpected schema object
    weird_obj = {"random_key": 12345}
    weird_caps = parse_claude_capacity(weird_obj, observed_at=now)
    assert len(weird_caps) == 1
    assert weird_caps[0].state == CapacityState.UNKNOWN

    # Invalid input type
    invalid_type_caps = parse_claude_capacity(12345, observed_at=now)  # type: ignore[arg-type]
    assert len(invalid_type_caps) == 1
    assert invalid_type_caps[0].state == CapacityState.UNKNOWN


def test_claude_capacity_sensor_protocol_and_execution_policy() -> None:
    now = datetime.now(UTC)
    unverified_policy = ClaudeExecutionPolicy(
        live_probe_opt_in=False,
        billing_mode=ClaudeBillingMode.UNKNOWN,
    )
    sensor: CapacitySensor = ClaudeCapacitySensor(execution_policy=unverified_policy)
    assert sensor.observe("codex", now=now) == ()
    caps = sensor.observe("claude", now=now)
    assert len(caps) == 1
    assert caps[0].state == CapacityState.DISABLED_BILLING

    verified_policy = ClaudeExecutionPolicy(
        live_probe_opt_in=True,
        billing_mode=ClaudeBillingMode.SUBSCRIPTION_VERIFIED,
    )
    verified_sensor = ClaudeCapacitySensor(execution_policy=verified_policy)
    verified_caps = verified_sensor.observe("claude", now=now)
    assert len(verified_caps) == 1
    assert verified_caps[0].state == CapacityState.AVAILABLE
