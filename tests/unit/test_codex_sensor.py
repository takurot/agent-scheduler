from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.capacity.base import CapacitySensor
from subsched.capacity.codex import CodexCapacitySensor, parse_codex_capacity
from subsched.models import CapacityState


def test_parse_codex_session_limit_fixture() -> None:
    payload = Path("tests/fixtures/codex/session-limit.jsonl").read_text(encoding="utf-8")
    now = datetime.now(UTC)

    capacities = parse_codex_capacity(payload, observed_at=now)
    assert len(capacities) == 1
    cap = capacities[0]
    assert cap.agent == "codex"
    assert cap.state == CapacityState.COOLDOWN_SESSION
    assert cap.scope == "five_hour"
    assert cap.source == "structured_result"
    assert cap.confidence == "high"
    assert cap.reset_at == datetime.fromisoformat("2026-08-13T02:00:00+00:00")


def test_parse_codex_weekly_limit_fixture() -> None:
    payload = Path("tests/fixtures/codex/weekly-limit.jsonl").read_text(encoding="utf-8")
    now = datetime.now(UTC)

    capacities = parse_codex_capacity(payload, observed_at=now)
    assert len(capacities) == 1
    cap = capacities[0]
    assert cap.agent == "codex"
    assert cap.state == CapacityState.COOLDOWN_WEEKLY
    assert cap.scope == "seven_day"
    assert cap.source == "structured_result"
    assert cap.confidence == "high"
    assert cap.reset_at == datetime.fromisoformat("2026-08-20T00:00:00+00:00")


def test_parse_codex_auth_and_billing_errors() -> None:
    now = datetime.now(UTC)

    auth_payload = Path("tests/fixtures/codex/auth-error.jsonl").read_text(encoding="utf-8")
    auth_caps = parse_codex_capacity(auth_payload, observed_at=now)
    assert len(auth_caps) == 1
    assert auth_caps[0].state == CapacityState.AUTH_ERROR

    billing_payload = Path("tests/fixtures/codex/billing-error.jsonl").read_text(encoding="utf-8")
    billing_caps = parse_codex_capacity(billing_payload, observed_at=now)
    assert len(billing_caps) == 1
    assert billing_caps[0].state == CapacityState.DISABLED_BILLING


def test_parse_codex_reset_unknown_fixture() -> None:
    payload = Path("tests/fixtures/codex/capacity-reset-unknown.jsonl").read_text(encoding="utf-8")
    now = datetime.now(UTC)

    capacities = parse_codex_capacity(payload, observed_at=now)
    assert len(capacities) == 1
    assert capacities[0].state == CapacityState.UNKNOWN


def test_parse_codex_success_fixture() -> None:
    payload = Path("tests/fixtures/codex/success.jsonl").read_text(encoding="utf-8")
    now = datetime.now(UTC)

    capacities = parse_codex_capacity(payload, observed_at=now)
    assert len(capacities) == 1
    assert capacities[0].state == CapacityState.AVAILABLE


def test_parse_codex_provider_rate_limits_json() -> None:
    now = datetime.now(UTC)
    t_5h = now + timedelta(hours=3)
    t_7d = now + timedelta(days=2)
    payload = {
        "rate_limits": {
            "five_hour": {
                "used_percentage": 50.0,
                "resets_at": int(t_5h.timestamp()),
            },
            "seven_day": {
                "used_percentage": 95.0,
                "resets_at": int(t_7d.timestamp()),
            },
        }
    }

    capacities = parse_codex_capacity(json.dumps(payload), observed_at=now)
    assert len(capacities) == 2
    by_scope = {c.scope: c for c in capacities}
    assert by_scope["five_hour"].state == CapacityState.AVAILABLE
    assert by_scope["seven_day"].state == CapacityState.PRESSURED_WEEKLY


def test_codex_capacity_sensor_protocol_and_policy() -> None:
    now = datetime.now(UTC)
    sensor_unverified: CapacitySensor = CodexCapacitySensor(
        allow_live=True,
        subscription_billing_verified=False,
    )
    assert sensor_unverified.observe("claude", now=now) == ()
    caps_unverified = sensor_unverified.observe("codex", now=now)
    assert len(caps_unverified) == 1
    assert caps_unverified[0].state == CapacityState.DISABLED_BILLING

    sensor_not_opted: CapacitySensor = CodexCapacitySensor(
        allow_live=False,
        subscription_billing_verified=True,
    )
    caps_not_opted = sensor_not_opted.observe("codex", now=now)
    assert len(caps_not_opted) == 1
    assert caps_not_opted[0].state == CapacityState.DISABLED

    sensor_verified: CapacitySensor = CodexCapacitySensor(
        allow_live=True,
        subscription_billing_verified=True,
    )
    caps_verified = sensor_verified.observe("codex", now=now)
    assert len(caps_verified) == 1
    assert caps_verified[0].state == CapacityState.AVAILABLE
