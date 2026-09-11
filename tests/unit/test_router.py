from datetime import UTC, datetime, timedelta

from subsched.models import Capacity, CapacityState
from subsched.router import FRESHNESS, AgentConfig, Router, _is_fresh_provider


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


def test_is_fresh_provider_rejects_future_observed_at() -> None:
    now = datetime.now(UTC)
    future = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        used_percentage=10,
        reset_at=now + timedelta(hours=1),
        observed_at=now + timedelta(seconds=1),
        source="provider",
        confidence="high",
    )

    assert _is_fresh_provider(future, now) is False


def test_is_fresh_provider_accepts_exactly_at_freshness_boundary() -> None:
    now = datetime.now(UTC)
    boundary = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        used_percentage=10,
        reset_at=now + timedelta(hours=1),
        observed_at=now - FRESHNESS,
        source="provider",
        confidence="high",
    )

    assert _is_fresh_provider(boundary, now) is True


def test_is_fresh_provider_rejects_beyond_freshness_boundary() -> None:
    now = datetime.now(UTC)
    stale = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        used_percentage=10,
        reset_at=now + timedelta(hours=1),
        observed_at=now - FRESHNESS - timedelta(seconds=1),
        source="provider",
        confidence="high",
    )

    assert _is_fresh_provider(stale, now) is False


def test_is_fresh_provider_accepts_missing_used_percentage() -> None:
    now = datetime.now(UTC)
    unpopulated = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        used_percentage=None,
        reset_at=now + timedelta(hours=1),
        observed_at=now,
        source="provider",
        confidence="high",
    )

    assert _is_fresh_provider(unpopulated, now) is True


def test_router_provider_capacity_with_missing_used_percentage_falls_back_to_priority() -> None:
    now = datetime.now(UTC)
    unpopulated_claude = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        used_percentage=None,
        reset_at=now + timedelta(hours=1),
        observed_at=now,
        source="provider",
        confidence="high",
    )
    unpopulated_codex = Capacity(
        agent="codex",
        state=CapacityState.AVAILABLE,
        used_percentage=None,
        reset_at=now + timedelta(hours=1),
        observed_at=now,
        source="provider",
        confidence="high",
    )
    router = Router(
        agents=(
            AgentConfig("claude", priority=100),
            AgentConfig("codex", priority=90),
        )
    )

    assert router.select((unpopulated_codex, unpopulated_claude), now=now) == "claude"


def test_router_prefers_fresh_provider_with_remaining_over_missing_used_percentage() -> None:
    now = datetime.now(UTC)
    unpopulated_claude = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        used_percentage=None,
        reset_at=now + timedelta(hours=1),
        observed_at=now,
        source="provider",
        confidence="high",
    )
    populated_codex = Capacity(
        agent="codex",
        state=CapacityState.AVAILABLE,
        used_percentage=50,
        reset_at=now + timedelta(hours=1),
        observed_at=now,
        source="provider",
        confidence="high",
    )
    router = Router(
        agents=(
            AgentConfig("claude", priority=100),
            AgentConfig("codex", priority=50),
        )
    )

    assert router.select((unpopulated_claude, populated_codex), now=now) == "codex"


def test_router_excludes_provider_capacity_observed_in_the_future() -> None:
    now = datetime.now(UTC)
    future = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        used_percentage=10,
        reset_at=now + timedelta(hours=1),
        observed_at=now + timedelta(seconds=1),
        source="provider",
        confidence="high",
    )
    router = Router(agents=(AgentConfig("claude", priority=100),))

    assert router.select((future,), now=now) is None


def test_router_excludes_stale_provider_capacity() -> None:
    now = datetime.now(UTC)
    stale = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        used_percentage=10,
        reset_at=now + timedelta(hours=1),
        observed_at=now - FRESHNESS - timedelta(seconds=1),
        source="provider",
        confidence="high",
    )
    router = Router(agents=(AgentConfig("claude", priority=100),))

    assert router.select((stale,), now=now) is None
