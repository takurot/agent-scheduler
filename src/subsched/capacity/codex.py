from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from subsched.agents.codex import parse_codex_jsonl
from subsched.models import AgentResultKind, Capacity, CapacityState


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def parse_codex_capacity(
    payload: str | dict[str, Any],
    *,
    returncode: int = 0,
    observed_at: datetime | None = None,
) -> tuple[Capacity, ...]:
    current = observed_at or datetime.now(UTC)

    if isinstance(payload, dict):
        data: Any = payload
        raw_str = json.dumps(payload)
    else:
        raw_str = payload
        try:
            data = json.loads(payload) if payload.strip().startswith("{") else None
        except Exception:
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
                    agent="codex",
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

    # Fallback to Codex JSONL parsing
    res = parse_codex_jsonl(raw_str, returncode=returncode)
    if res.kind is AgentResultKind.PASS:
        return (
            Capacity(
                agent="codex",
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
                agent="codex",
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
                agent="codex",
                state=CapacityState.COOLDOWN_WEEKLY,
                scope="seven_day",
                reset_at=res.reset_at,
                observed_at=current,
                source="structured_result",
                confidence="high",
            ),
        )

    # Check failure output for specific classifications
    msg = res.output.casefold()
    if "authentication" in msg or "unauthorized" in msg or "login" in msg:
        return (
            Capacity(
                agent="codex",
                state=CapacityState.AUTH_ERROR,
                scope="overall",
                observed_at=current,
                source="structured_result",
                confidence="high",
            ),
        )
    if "billing" in msg or "payment" in msg:
        return (
            Capacity(
                agent="codex",
                state=CapacityState.DISABLED_BILLING,
                scope="overall",
                observed_at=current,
                source="structured_result",
                confidence="high",
            ),
        )
    if "reset unknown" in msg:
        return (
            Capacity(
                agent="codex",
                state=CapacityState.UNKNOWN,
                scope="overall",
                observed_at=current,
                source="unknown",
                confidence="low",
            ),
        )

    return (
        Capacity(
            agent="codex",
            state=CapacityState.UNKNOWN,
            scope="overall",
            observed_at=current,
            source="unknown",
            confidence="low",
        ),
    )


class CodexCapacitySensor:
    """Codex capacity sensor observing provider status or policy."""

    def __init__(
        self,
        allow_live: bool = False,
        subscription_billing_verified: bool = False,
    ) -> None:
        self.allow_live = allow_live
        self.subscription_billing_verified = subscription_billing_verified

    def observe(self, agent: str, *, now: datetime | None = None) -> tuple[Capacity, ...]:
        if agent != "codex":
            return ()
        current = now or datetime.now(UTC)
        if not self.allow_live:
            return (
                Capacity(
                    agent="codex",
                    state=CapacityState.DISABLED,
                    scope="overall",
                    observed_at=current,
                    source="provider",
                    confidence="high",
                ),
            )
        if not self.subscription_billing_verified:
            return (
                Capacity(
                    agent="codex",
                    state=CapacityState.DISABLED_BILLING,
                    scope="overall",
                    observed_at=current,
                    source="provider",
                    confidence="high",
                ),
            )
        return (
            Capacity(
                agent="codex",
                state=CapacityState.AVAILABLE,
                scope="overall",
                observed_at=current,
                source="provider",
                confidence="high",
            ),
        )
