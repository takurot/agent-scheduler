from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from subsched.models import Capacity, CapacityState
from subsched.router import FRESHNESS

SOURCE_PRIORITY: dict[str, int] = {
    "provider": 1,
    "structured_result": 2,
    "structured_cli": 2,
    "reset_text": 3,
    "reset_message": 3,
    "exit_code": 4,
    "output_classification": 5,
    "stderr_stdout": 5,
    "telemetry": 6,
    "ccusage": 6,
    "historical": 7,
    "unknown": 8,
}

BLOCKER_SEVERITY: dict[CapacityState, int] = {
    CapacityState.DISABLED_BILLING: 100,
    CapacityState.AUTH_ERROR: 95,
    CapacityState.DISABLED: 90,
    CapacityState.FAILED: 85,
    CapacityState.COOLDOWN_WEEKLY: 70,
    CapacityState.COOLDOWN_SESSION: 60,
    CapacityState.COOLDOWN_MODEL: 50,
    CapacityState.RATE_LIMITED_TEMPORARY: 40,
    CapacityState.UNKNOWN: 30,
    CapacityState.BUSY: 20,
    CapacityState.PRESSURED_WEEKLY: 12,
    CapacityState.PRESSURED_SESSION: 10,
    CapacityState.AVAILABLE: 0,
}


def get_source_priority(source: str) -> int:
    return SOURCE_PRIORITY.get(source.casefold(), 8)


def canonicalize_scope(scope: str | None) -> str:
    if scope is None:
        return "overall"
    s = scope.strip().casefold()
    if s in {"five_hour", "5h", "session", "five-hour"}:
        return "five_hour"
    if s in {"seven_day", "7d", "weekly", "seven-day"}:
        return "seven_day"
    if s in {"model"}:
        return "model"
    if s in {"temporary", "temp"}:
        return "temporary"
    if s in {"overall", "global", "default"}:
        return "overall"
    return s


class CapacitySensor(Protocol):
    """Protocol for capacity sensors."""

    def observe(self, agent: str, *, now: datetime | None = None) -> tuple[Capacity, ...]:
        """Observe one or more capacity readings for the given agent."""
        ...


@dataclass
class AgentCapacityRecord:
    agent: str
    windows: dict[str, Capacity] = field(default_factory=dict)

    def update(self, capacity: Capacity, *, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        scope = canonicalize_scope(capacity.scope)
        existing = self.windows.get(scope)
        if existing is None:
            self.windows[scope] = capacity
            return

        new_priority = get_source_priority(capacity.source)
        existing_priority = get_source_priority(existing.source)

        existing_stale = (
            existing.source == "provider"
            and (current - existing.observed_at > FRESHNESS)
        )

        newer_direct_result = (
            new_priority <= 3
            and capacity.observed_at > existing.observed_at
        )

        should_update = (
            new_priority < existing_priority
            or existing_stale
            or newer_direct_result
            or (new_priority == existing_priority and capacity.observed_at >= existing.observed_at)
        )
        if should_update:
            self.windows[scope] = capacity

    def effective_capacity(self, *, now: datetime | None = None) -> Capacity | None:
        if not self.windows:
            return None
        current = now or datetime.now(UTC)
        strongest = select_strongest_blocker(self.windows.values(), now=current)
        if strongest is not None and strongest.state != CapacityState.AVAILABLE:
            return strongest
        return max(
            self.windows.values(),
            key=lambda c: (
                c.used_percentage if c.used_percentage is not None else -1.0,
                c.observed_at,
            ),
        )

    def earliest_reset(self, *, now: datetime | None = None) -> datetime | None:
        return find_earliest_reset(self.windows.values(), now=now)


def select_strongest_blocker(
    capacities: Iterable[Capacity], *, now: datetime | None = None
) -> Capacity | None:
    items = tuple(capacities)
    if not items:
        return None

    def blocker_key(c: Capacity) -> tuple[int, int, datetime, str]:
        severity = BLOCKER_SEVERITY.get(c.state, 0)
        prio = -get_source_priority(c.source)  # higher priority source preferred on tie
        return (severity, prio, c.observed_at, c.agent)

    return max(items, key=blocker_key)


def find_earliest_reset(
    capacities: Iterable[Capacity], *, now: datetime | None = None
) -> datetime | None:
    current = now or datetime.now(UTC)
    resets = [
        c.reset_at
        for c in capacities
        if c.reset_at is not None and c.reset_at > current
    ]
    return min(resets) if resets else None


def merge_capacities(
    existing: Iterable[Capacity],
    incoming: Iterable[Capacity],
    *,
    now: datetime | None = None,
) -> tuple[Capacity, ...]:
    records: dict[str, AgentCapacityRecord] = {}
    for c in existing:
        if c.agent not in records:
            records[c.agent] = AgentCapacityRecord(agent=c.agent)
        records[c.agent].update(c, now=now)
    for c in incoming:
        if c.agent not in records:
            records[c.agent] = AgentCapacityRecord(agent=c.agent)
        records[c.agent].update(c, now=now)

    merged: list[Capacity] = []
    for record in records.values():
        merged.extend(record.windows.values())
    return tuple(merged)
