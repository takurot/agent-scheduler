from __future__ import annotations

from datetime import UTC, datetime, timedelta

from subsched.capacity.base import (
    AgentCapacityRecord,
    CapacitySensor,
    canonicalize_scope,
    find_earliest_reset,
    get_source_priority,
    merge_capacities,
    select_strongest_blocker,
)
from subsched.models import Capacity, CapacityState


class FakeSensor:
    def __init__(self, capacities: tuple[Capacity, ...]) -> None:
        self._capacities = capacities

    def observe(self, agent: str, *, now: datetime | None = None) -> tuple[Capacity, ...]:
        return tuple(c for c in self._capacities if c.agent == agent)


def test_sensor_interface_protocol() -> None:
    now = datetime.now(UTC)
    cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=now,
        source="provider",
        confidence="high",
        scope="five_hour",
    )
    sensor: CapacitySensor = FakeSensor((cap,))
    readings = sensor.observe("claude", now=now)
    assert len(readings) == 1
    assert readings[0].agent == "claude"


def test_source_priority_ordering() -> None:
    assert get_source_priority("provider") < get_source_priority("structured_result")
    assert get_source_priority("structured_result") < get_source_priority("reset_text")
    assert get_source_priority("reset_text") < get_source_priority("exit_code")
    assert get_source_priority("exit_code") < get_source_priority("output_classification")
    assert get_source_priority("output_classification") < get_source_priority("telemetry")
    assert get_source_priority("telemetry") < get_source_priority("historical")
    assert get_source_priority("historical") < get_source_priority("unknown")
    assert get_source_priority("unrecognized_source") == 8


def test_canonicalize_scope() -> None:
    assert canonicalize_scope("five_hour") == "five_hour"
    assert canonicalize_scope("5h") == "five_hour"
    assert canonicalize_scope("session") == "five_hour"
    assert canonicalize_scope("seven_day") == "seven_day"
    assert canonicalize_scope("7d") == "seven_day"
    assert canonicalize_scope("weekly") == "seven_day"
    assert canonicalize_scope("model") == "model"
    assert canonicalize_scope("temporary") == "temporary"
    assert canonicalize_scope("temp") == "temporary"
    assert canonicalize_scope("global") == "overall"
    assert canonicalize_scope(None) == "overall"
    assert canonicalize_scope("custom_scope") == "custom_scope"


def test_agent_capacity_record_maintains_multiple_windows() -> None:
    now = datetime.now(UTC)
    record = AgentCapacityRecord(agent="claude")

    cap_session = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        used_percentage=45.0,
        observed_at=now,
        source="provider",
        confidence="high",
    )
    cap_weekly = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        scope="seven_day",
        used_percentage=80.0,
        observed_at=now,
        source="provider",
        confidence="high",
    )

    record.update(cap_session)
    record.update(cap_weekly)

    assert len(record.windows) == 2
    assert "five_hour" in record.windows
    assert "seven_day" in record.windows


def test_source_priority_overrides_lower_priority() -> None:
    now = datetime.now(UTC)
    record = AgentCapacityRecord(agent="claude")

    telemetry_cap = Capacity(
        agent="claude",
        state=CapacityState.PRESSURED_SESSION,
        scope="five_hour",
        used_percentage=90.0,
        observed_at=now,
        source="telemetry",
        confidence="low",
    )
    provider_cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        used_percentage=40.0,
        observed_at=now,
        source="provider",
        confidence="high",
    )

    record.update(telemetry_cap)
    assert record.windows["five_hour"].used_percentage == 90.0

    # Provider should override telemetry
    record.update(provider_cap)
    assert record.windows["five_hour"].used_percentage == 40.0
    assert record.windows["five_hour"].source == "provider"

    # Lower priority should not overwrite provider unless stale
    record.update(telemetry_cap)
    assert record.windows["five_hour"].used_percentage == 40.0

    # If provider reading becomes stale, telemetry can update it
    future = now + timedelta(minutes=10)
    fresh_telemetry = Capacity(
        agent="claude",
        state=CapacityState.COOLDOWN_SESSION,
        scope="five_hour",
        used_percentage=100.0,
        observed_at=future,
        source="telemetry",
        confidence="low",
    )
    record.update(fresh_telemetry, now=future)
    assert record.windows["five_hour"].state == CapacityState.COOLDOWN_SESSION


def test_agent_capacity_record_effective_capacity_and_earliest_reset() -> None:
    now = datetime.now(UTC)
    record = AgentCapacityRecord(agent="codex")
    assert record.effective_capacity() is None
    assert record.earliest_reset() is None

    cap_avail_1 = Capacity(
        agent="codex",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        used_percentage=30.0,
        observed_at=now,
        source="provider",
        confidence="high",
    )
    cap_avail_2 = Capacity(
        agent="codex",
        state=CapacityState.AVAILABLE,
        scope="seven_day",
        used_percentage=75.0,
        observed_at=now,
        source="provider",
        confidence="high",
    )
    record.update(cap_avail_1)
    record.update(cap_avail_2)

    eff = record.effective_capacity()
    assert eff is not None
    assert eff.scope == "seven_day"
    assert eff.used_percentage == 75.0

    # Now add a cooldown window from execution after the initial observation
    exec_time = now + timedelta(seconds=10)
    cap_cooldown = Capacity(
        agent="codex",
        state=CapacityState.COOLDOWN_SESSION,
        scope="five_hour",
        reset_at=exec_time + timedelta(minutes=45),
        observed_at=exec_time,
        source="structured_result",
        confidence="high",
    )
    record.update(cap_cooldown)
    eff_blocked = record.effective_capacity()
    assert eff_blocked is not None
    assert eff_blocked.state == CapacityState.COOLDOWN_SESSION
    assert record.earliest_reset(now=now) == exec_time + timedelta(minutes=45)


def test_select_strongest_blocker() -> None:
    assert select_strongest_blocker([]) is None

    now = datetime.now(UTC)
    cap_avail = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        observed_at=now,
        source="provider",
        confidence="high",
    )
    cap_session_cooldown = Capacity(
        agent="claude",
        state=CapacityState.COOLDOWN_SESSION,
        scope="five_hour",
        reset_at=now + timedelta(hours=2),
        observed_at=now,
        source="structured_result",
        confidence="high",
    )
    cap_weekly_cooldown = Capacity(
        agent="claude",
        state=CapacityState.COOLDOWN_WEEKLY,
        scope="seven_day",
        reset_at=now + timedelta(days=3),
        observed_at=now,
        source="structured_result",
        confidence="high",
    )
    cap_auth_error = Capacity(
        agent="claude",
        state=CapacityState.AUTH_ERROR,
        scope="overall",
        observed_at=now,
        source="structured_result",
        confidence="high",
    )

    # Weekly cooldown is stronger than session cooldown
    strongest = select_strongest_blocker([cap_avail, cap_session_cooldown, cap_weekly_cooldown])
    assert strongest is not None
    assert strongest.state == CapacityState.COOLDOWN_WEEKLY

    # Auth error is stronger than weekly cooldown
    strongest_fatal = select_strongest_blocker(
        [cap_avail, cap_session_cooldown, cap_weekly_cooldown, cap_auth_error]
    )
    assert strongest_fatal is not None
    assert strongest_fatal.state == CapacityState.AUTH_ERROR


def test_find_earliest_reset() -> None:
    assert find_earliest_reset([]) is None

    now = datetime.now(UTC)
    t1 = now + timedelta(hours=1)
    t2 = now + timedelta(hours=3)
    past = now - timedelta(hours=1)

    c1 = Capacity(
        agent="claude",
        state=CapacityState.COOLDOWN_SESSION,
        scope="five_hour",
        reset_at=t2,
        observed_at=now,
        source="structured_result",
        confidence="high",
    )
    c2 = Capacity(
        agent="codex",
        state=CapacityState.COOLDOWN_SESSION,
        scope="five_hour",
        reset_at=t1,
        observed_at=now,
        source="structured_result",
        confidence="high",
    )
    c_past = Capacity(
        agent="other",
        state=CapacityState.COOLDOWN_SESSION,
        scope="five_hour",
        reset_at=past,
        observed_at=now,
        source="structured_result",
        confidence="high",
    )
    c_no_reset = Capacity(
        agent="claude",
        state=CapacityState.AUTH_ERROR,
        scope="overall",
        reset_at=None,
        observed_at=now,
        source="structured_result",
        confidence="high",
    )

    earliest = find_earliest_reset([c1, c2, c_past, c_no_reset], now=now)
    assert earliest == t1


def test_merge_capacities() -> None:
    assert merge_capacities([], []) == ()

    now = datetime.now(UTC)
    c1 = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        used_percentage=30.0,
        observed_at=now,
        source="provider",
        confidence="high",
    )
    c2 = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        scope="seven_day",
        used_percentage=70.0,
        observed_at=now,
        source="provider",
        confidence="high",
    )
    c1_update = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        scope="five_hour",
        used_percentage=35.0,
        observed_at=now + timedelta(seconds=10),
        source="provider",
        confidence="high",
    )

    merged = merge_capacities([c1, c2], [c1_update])
    merged_map = {(c.agent, c.scope): c for c in merged}
    assert len(merged) == 2
    assert merged_map[("claude", "five_hour")].used_percentage == 35.0
    assert merged_map[("claude", "seven_day")].used_percentage == 70.0
