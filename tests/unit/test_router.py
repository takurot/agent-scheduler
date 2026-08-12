from datetime import UTC, datetime, timedelta

from subsched.models import Capacity, CapacityState
from subsched.router import AgentConfig, Router


def capacity(agent: str, used: float, *, source: str = "provider") -> Capacity:
    now = datetime.now(UTC)
    return Capacity(
        agent=agent,
        state=CapacityState.AVAILABLE,
        used_percentage=used,
        reset_at=now + timedelta(hours=1),
        observed_at=now,
        source=source,
        confidence="high",
    )


def test_router_prefers_fresh_provider_remaining_capacity() -> None:
    router = Router(agents=(AgentConfig("claude", priority=100), AgentConfig("codex", priority=90)))

    selected = router.select((capacity("claude", 94), capacity("codex", 32)))

    assert selected == "codex"


def test_router_uses_static_priority_when_capacity_is_unknown() -> None:
    now = datetime.now(UTC)
    unknown = tuple(
        Capacity(
            agent=name,
            state=CapacityState.AVAILABLE,
            observed_at=now,
            source="local_estimate",
            confidence="low",
        )
        for name in ("claude", "codex")
    )
    router = Router(agents=(AgentConfig("claude", priority=100), AgentConfig("codex", priority=90)))

    assert router.select(unknown) == "claude"


def test_router_returns_none_when_all_agents_are_unavailable() -> None:
    now = datetime.now(UTC)
    unavailable = (
        Capacity(
            agent="claude",
            state=CapacityState.COOLDOWN_SESSION,
            reset_at=now + timedelta(hours=1),
            observed_at=now,
            source="provider",
            confidence="high",
        ),
    )

    assert Router((AgentConfig("claude", 100),)).select(unavailable) is None
