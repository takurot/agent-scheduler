from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from subsched.capacity.base import canonicalize_scope
from subsched.models import Capacity, CapacityState


def parse_ccusage_output(
    raw_output: str,
    *,
    agent: str = "claude",
    observed_at: datetime | None = None,
) -> tuple[Capacity, ...]:
    current = observed_at or datetime.now(UTC)
    if not raw_output.strip():
        return ()

    try:
        data: Any = json.loads(raw_output)
    except (json.JSONDecodeError, UnicodeError):
        return ()

    if not isinstance(data, dict):
        return ()

    agent_data = data.get(agent)
    if not isinstance(agent_data, dict):
        # Check if root dict has session/weekly directly
        if "session" in data or "weekly" in data or "five_hour" in data:
            agent_data = data
        else:
            return ()

    results: list[Capacity] = []
    for scope_key, val in agent_data.items():
        if not isinstance(val, dict):
            continue
        used = val.get("estimated_percentage")
        if used is None:
            used = val.get("used_percentage")
        if not isinstance(used, (int, float)) or not 0 <= used <= 100:
            continue
        used_pct = float(used)
        canonical_scope = canonicalize_scope(scope_key)

        # Telemetry only produces AVAILABLE or PRESSURED, never hard COOLDOWN or DISABLED
        if used_pct >= 80.0:
            state = (
                CapacityState.PRESSURED_SESSION
                if canonical_scope == "five_hour"
                else CapacityState.PRESSURED_WEEKLY
            )
        else:
            state = CapacityState.AVAILABLE

        results.append(
            Capacity(
                agent=agent,
                state=state,
                scope=canonical_scope,
                used_percentage=used_pct,
                observed_at=current,
                source="telemetry",
                confidence="low",
            )
        )

    return tuple(results)


class CcusageSensor:
    """Optional secondary telemetry capacity sensor using ccusage."""

    def __init__(
        self,
        *,
        available: bool = False,
        command: str = "ccusage",
        runner: Callable[[], str] | None = None,
    ) -> None:
        self.available = available
        self.command = command
        self.runner = runner

    def observe(self, agent: str, *, now: datetime | None = None) -> tuple[Capacity, ...]:
        if not self.available:
            return ()
        current = now or datetime.now(UTC)
        if self.runner is None:
            return ()
        try:
            raw = self.runner()
        except Exception:
            return ()
        return parse_ccusage_output(raw, agent=agent, observed_at=current)
