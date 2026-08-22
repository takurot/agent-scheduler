from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from subsched.agents.claude import (
    ClaudeBillingMode,
    ClaudeExecutionPolicy,
    ClaudeProcessOutcome,
    parse_claude_result,
)
from subsched.models import AgentResultKind, Capacity, CapacityState


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def parse_claude_capacity(
    payload_or_outcome: str | dict[str, Any] | ClaudeProcessOutcome,
    *,
    observed_at: datetime | None = None,
) -> tuple[Capacity, ...]:
    current = observed_at or datetime.now(UTC)

    if isinstance(payload_or_outcome, ClaudeProcessOutcome):
        outcome = payload_or_outcome
    elif isinstance(payload_or_outcome, dict):
        exit_code = 1 if payload_or_outcome.get("is_error") is True else 0
        outcome = ClaudeProcessOutcome(
            exit_code=exit_code,
            stdout=json.dumps(payload_or_outcome),
            stderr="",
        )
    elif isinstance(payload_or_outcome, str):
        try:
            parsed = json.loads(payload_or_outcome)
            exit_code = 1 if isinstance(parsed, dict) and parsed.get("is_error") is True else 0
        except Exception:
            exit_code = 0
        outcome = ClaudeProcessOutcome(
            exit_code=exit_code,
            stdout=payload_or_outcome,
            stderr="",
        )
    else:
        return (
            Capacity(
                agent="claude",
                state=CapacityState.UNKNOWN,
                observed_at=current,
                source="unknown",
                confidence="low",
            ),
        )

    # 1. Try parsing provider structured rate_limits JSON
    try:
        data = json.loads(outcome.stdout) if outcome.stdout else None
    except (json.JSONDecodeError, UnicodeError):
        data = None

    if isinstance(data, dict) and "rate_limits" in data and isinstance(data["rate_limits"], dict):
        rate_limits = data["rate_limits"]
        results: list[Capacity] = []
        for scope_key, window_data in rate_limits.items():
            if not isinstance(window_data, dict):
                continue
            used = window_data.get("used_percentage")
            if not isinstance(used, (int, float)) or not 0 <= used <= 100:
                continue
            used_pct = float(used)
            reset_at = _parse_timestamp(window_data.get("resets_at"))
            canonical_scope = (
                "five_hour" if scope_key in {"five_hour", "5h", "session"} else "seven_day"
            )

            if used_pct >= 100.0:
                state = (
                    CapacityState.COOLDOWN_SESSION
                    if canonical_scope == "five_hour"
                    else CapacityState.COOLDOWN_WEEKLY
                )
            elif used_pct >= 80.0:
                state = (
                    CapacityState.PRESSURED_SESSION
                    if canonical_scope == "five_hour"
                    else CapacityState.PRESSURED_WEEKLY
                )
            else:
                state = CapacityState.AVAILABLE

            results.append(
                Capacity(
                    agent="claude",
                    state=state,
                    scope=canonical_scope,
                    used_percentage=used_pct,
                    reset_at=reset_at,
                    observed_at=current,
                    source="provider",
                    confidence="high",
                )
            )
        if results:
            return tuple(results)

    # 2. Fall back to standard result parsing
    res = parse_claude_result(outcome)
    if res.kind is AgentResultKind.PASS:
        return (
            Capacity(
                agent="claude",
                state=CapacityState.AVAILABLE,
                scope="overall",
                observed_at=current,
                source="structured_result",
                confidence="high",
            ),
        )
    if res.kind is AgentResultKind.CAPACITY_SESSION:
        return (
            Capacity(
                agent="claude",
                state=CapacityState.COOLDOWN_SESSION,
                scope="five_hour",
                reset_at=res.reset_at,
                observed_at=current,
                source="structured_result",
                confidence="high",
            ),
        )
    if res.kind is AgentResultKind.CAPACITY_WEEKLY:
        return (
            Capacity(
                agent="claude",
                state=CapacityState.COOLDOWN_WEEKLY,
                scope="seven_day",
                reset_at=res.reset_at,
                observed_at=current,
                source="structured_result",
                confidence="high",
            ),
        )
    if res.kind is AgentResultKind.CAPACITY_TEMPORARY:
        return (
            Capacity(
                agent="claude",
                state=CapacityState.RATE_LIMITED_TEMPORARY,
                scope="temporary",
                observed_at=current,
                source="structured_result",
                confidence="high",
            ),
        )
    if res.kind is AgentResultKind.AUTH_ERROR:
        return (
            Capacity(
                agent="claude",
                state=CapacityState.AUTH_ERROR,
                scope="overall",
                observed_at=current,
                source="structured_result",
                confidence="high",
            ),
        )
    if res.kind in {AgentResultKind.BILLING_ERROR, AgentResultKind.UNKNOWN_BILLING}:
        return (
            Capacity(
                agent="claude",
                state=CapacityState.DISABLED_BILLING,
                scope="overall",
                observed_at=current,
                source="structured_result",
                confidence="high",
            ),
        )

    # Schema drift or unknown failure
    return (
        Capacity(
            agent="claude",
            state=CapacityState.UNKNOWN,
            scope="overall",
            observed_at=current,
            source="unknown",
            confidence="low",
        ),
    )


class ClaudeCapacitySensor:
    """Claude capacity sensor observing provider status or policy."""

    def __init__(
        self,
        execution_policy: ClaudeExecutionPolicy | None = None,
    ) -> None:
        self.execution_policy = execution_policy or ClaudeExecutionPolicy(
            live_probe_opt_in=False,
            billing_mode=ClaudeBillingMode.UNKNOWN,
        )

    def observe(self, agent: str, *, now: datetime | None = None) -> tuple[Capacity, ...]:
        if agent != "claude":
            return ()
        current = now or datetime.now(UTC)
        if (
            not self.execution_policy.live_probe_opt_in
            or self.execution_policy.billing_mode != ClaudeBillingMode.SUBSCRIPTION_VERIFIED
        ):
            return (
                Capacity(
                    agent="claude",
                    state=self.execution_policy.worker_state,
                    scope="overall",
                    observed_at=current,
                    source="provider",
                    confidence="high",
                ),
            )
        return (
            Capacity(
                agent="claude",
                state=CapacityState.AVAILABLE,
                scope="overall",
                observed_at=current,
                source="provider",
                confidence="high",
            ),
        )
