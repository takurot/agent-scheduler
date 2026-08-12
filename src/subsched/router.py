from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from subsched.models import Capacity

FRESHNESS = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    name: str
    priority: int
    enabled: bool = True


class Router:
    def __init__(self, agents: Iterable[AgentConfig]) -> None:
        configs = tuple(agents)
        self._agents = {agent.name: agent for agent in configs}
        if len(self._agents) != len(configs):
            raise ValueError("duplicate agent configuration")

    def select(self, capacities: Iterable[Capacity], *, now: datetime | None = None) -> str | None:
        current = now or datetime.now(UTC)
        candidates = [
            capacity
            for capacity in capacities
            if capacity.agent in self._agents
            and self._agents[capacity.agent].enabled
            and capacity.is_available(current)
            and (
                capacity.source != "provider"
                or timedelta(0) <= current - capacity.observed_at <= FRESHNESS
            )
        ]
        if not candidates:
            return None

        def score(capacity: Capacity) -> tuple[int, float, int, str]:
            fresh_provider = (
                capacity.source == "provider"
                and capacity.used_percentage is not None
                and current - capacity.observed_at <= FRESHNESS
            )
            remaining = capacity.remaining_percentage if fresh_provider else 0.0
            return (
                int(fresh_provider),
                remaining or 0.0,
                self._agents[capacity.agent].priority,
                capacity.agent,
            )

        return max(candidates, key=score).agent
