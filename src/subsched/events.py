from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    def __init__(self, initial: datetime | None = None) -> None:
        self._current = initial or datetime(2026, 8, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current += delta

    def set_time(self, new_time: datetime) -> None:
        self._current = new_time


class EventType(StrEnum):
    CAPACITY_PROBE = "CAPACITY_PROBE"
    CAPACITY_RESET = "CAPACITY_RESET"
    TASK_COMPLETED = "TASK_COMPLETED"
    PAUSE = "PAUSE"
    RESUME = "RESUME"


@dataclass(frozen=True, slots=True)
class Event:
    event_type: EventType
    timestamp: datetime
    payload: dict[str, Any] = field(default_factory=dict)


class EventSource(Protocol):
    def poll(self, now: datetime) -> tuple[Event, ...]:
        ...


class FakeEventSource:
    def __init__(self, events: Iterable[Event] = ()) -> None:
        self._events: list[Event] = list(events)

    def emit(self, event: Event) -> None:
        self._events.append(event)

    def poll(self, now: datetime) -> tuple[Event, ...]:
        ready = [e for e in self._events if e.timestamp <= now]
        self._events = [e for e in self._events if e.timestamp > now]
        return tuple(ready)
