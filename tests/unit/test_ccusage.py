from __future__ import annotations

import json
from datetime import UTC, datetime

from subsched.capacity.base import CapacitySensor
from subsched.capacity.ccusage import CcusageSensor, parse_ccusage_output
from subsched.models import CapacityState


def test_parse_ccusage_output_valid_json() -> None:
    now = datetime.now(UTC)
    payload = {
        "claude": {
            "session": {
                "input_tokens": 15000,
                "output_tokens": 4000,
                "estimated_percentage": 35.0,
            },
            "weekly": {
                "input_tokens": 120000,
                "output_tokens": 30000,
                "used_percentage": 60.0,
            },
        }
    }

    caps = parse_ccusage_output(json.dumps(payload), agent="claude", observed_at=now)
    assert len(caps) == 2
    by_scope = {c.scope: c for c in caps}
    assert "five_hour" in by_scope
    assert "seven_day" in by_scope

    cap_session = by_scope["five_hour"]
    assert cap_session.agent == "claude"
    assert cap_session.source == "telemetry"
    assert cap_session.confidence == "low"
    assert cap_session.used_percentage == 35.0
    assert cap_session.state == CapacityState.AVAILABLE


def test_parse_ccusage_high_usage_does_not_disable_worker() -> None:
    now = datetime.now(UTC)
    payload = {
        "claude": {
            "session": {
                "estimated_percentage": 98.0,
            },
            "weekly": {
                "estimated_percentage": 85.0,
            },
        }
    }

    caps = parse_ccusage_output(json.dumps(payload), agent="claude", observed_at=now)
    assert len(caps) == 2
    assert caps[0].state == CapacityState.PRESSURED_SESSION
    assert caps[0].source == "telemetry"
    assert caps[0].confidence == "low"
    assert caps[1].state == CapacityState.PRESSURED_WEEKLY


def test_parse_ccusage_malformed_input() -> None:
    now = datetime.now(UTC)
    assert parse_ccusage_output("", agent="claude", observed_at=now) == ()
    assert parse_ccusage_output("not json", agent="claude", observed_at=now) == ()
    assert parse_ccusage_output("[]", agent="claude", observed_at=now) == ()
    assert parse_ccusage_output("{}", agent="claude", observed_at=now) == ()
    assert parse_ccusage_output(json.dumps({"other": {}}), agent="claude", observed_at=now) == ()
    assert (
        parse_ccusage_output(
            json.dumps({"claude": {"session": "invalid_val"}}), agent="claude", observed_at=now
        )
        == ()
    )
    assert (
        parse_ccusage_output(
            json.dumps({"claude": {"session": {"estimated_percentage": 150.0}}}),
            agent="claude",
            observed_at=now,
        )
        == ()
    )


def test_ccusage_sensor_when_unavailable() -> None:
    now = datetime.now(UTC)
    sensor: CapacitySensor = CcusageSensor(available=False)
    assert sensor.observe("claude", now=now) == ()
    assert sensor.observe("codex", now=now) == ()


def test_ccusage_sensor_with_runner() -> None:
    now = datetime.now(UTC)
    payload = json.dumps(
        {
            "session": {
                "estimated_percentage": 25.0,
            }
        }
    )

    def fake_runner() -> str:
        return payload

    sensor: CapacitySensor = CcusageSensor(available=True, runner=fake_runner)
    caps = sensor.observe("claude", now=now)
    assert len(caps) == 1
    assert caps[0].source == "telemetry"
    assert caps[0].used_percentage == 25.0

    # Test runner error handling
    def error_runner() -> str:
        raise RuntimeError("ccusage error")

    error_sensor: CapacitySensor = CcusageSensor(available=True, runner=error_runner)
    assert error_sensor.observe("claude", now=now) == ()

    # Test sensor without runner
    no_runner_sensor: CapacitySensor = CcusageSensor(available=True, runner=None)
    assert no_runner_sensor.observe("claude", now=now) == ()
