from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from subsched.agents.claude import ClaudeProcessOutcome, parse_claude_result
from subsched.agents.codex import parse_codex_jsonl
from subsched.events import Event, EventType, FakeEventSource
from subsched.models import AgentResult, AgentResultKind, Capacity, CapacityState, Issue, TaskState
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore


@pytest.mark.parametrize("alternative", [False, True])
@pytest.mark.parametrize(
    ("fixture", "blocker"),
    [
        ("auth-error.jsonl", CapacityState.AUTH_ERROR),
        ("billing-error.jsonl", CapacityState.DISABLED_BILLING),
        ("approval-error.jsonl", None),
        ("capacity-reset-unknown.jsonl", None),
    ],
)
def test_codex_safety_results_do_not_retry_or_consume_failure_budget(
    tmp_path: Path, fixture: str, blocker: CapacityState | None, alternative: bool
) -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    payload = (Path(__file__).parents[1] / "fixtures" / "codex" / fixture).read_text()
    worker = ScriptedWorker(
        {
            (450, "codex"): (parse_codex_jsonl(payload, returncode=1),),
            (450, "claude"): (AgentResult(AgentResultKind.PASS),),
        }
    )
    agents = (AgentConfig("codex", 100),)
    if alternative:
        agents += (AgentConfig("claude", 90),)
    router = Router(agents)
    store = JsonStateStore(tmp_path / "state.json")
    scheduler = Scheduler(
        store=store, router=router, worker=worker, worktree_root=tmp_path / "worktrees"
    )
    scheduler.discover((Issue(number=450, title="Codex safety results"),))
    capacities = tuple(
        Capacity(
            agent=agent.name,
            state=CapacityState.AVAILABLE,
            reset_at=now + timedelta(hours=5),
            observed_at=now,
            source="provider",
            confidence="high",
        )
        for agent in agents
    )

    assert scheduler.tick(capacities, now=now)
    task = scheduler.tasks[0]
    failover = alternative and blocker is not None
    assert task.status is (TaskState.READY if failover else TaskState.NEEDS_HUMAN)
    assert task.attempt == 0
    assert task.per_agent_failures == ()
    assert task.verification_failures == 0
    assert task.capacity_events == 0
    assert task.agent_switches == 0
    assert task.actual_agent_switches == 0
    if blocker is None:
        assert "codex" not in scheduler._cooldowns
        if fixture == "capacity-reset-unknown.jsonl":
            assert task.needs_human_reason_code == "external_prerequisite"
            assert "verify provider capacity" in (task.needs_human_reason or "")
        else:
            assert "permission denied" in (task.needs_human_reason or "")
    else:
        cooldown = scheduler._cooldowns["codex"]
        assert cooldown.state is blocker
        assert cooldown.reset_at is None
        assert cooldown.source == "structured_result"
        assert cooldown.confidence == "high"
    assert store.load_tasks() == scheduler.tasks

    recovered = Scheduler(
        store=store, router=router, worker=worker, worktree_root=tmp_path / "worktrees"
    )
    recovered.refresh_capacities(capacities, now=now)
    if blocker is not None:
        assert recovered._cooldowns["codex"].state is blocker
    assert recovered.tick(capacities, now=now) is failover
    final = recovered.tasks[0]
    assert final.worktree == task.worktree
    assert final.attempt == 0
    assert final.per_agent_failures == ()
    assert final.actual_agent_switches == int(failover)
    expected_dispatches = [(450, "codex"), (450, "claude")] if failover else [(450, "codex")]
    assert worker.dispatches == expected_dispatches


@pytest.mark.parametrize(
    "blocker", [None, CapacityState.AUTH_ERROR, CapacityState.DISABLED_BILLING]
)
def test_unknown_probe_does_not_create_or_replace_cooldown(
    tmp_path: Path, blocker: CapacityState | None
) -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    events = FakeEventSource()
    store = JsonStateStore(tmp_path / "state.json")
    if blocker is not None:
        store.save_state((), capacities=(Capacity(
            agent="codex", state=blocker, observed_at=now,
            source="structured_result", confidence="high",
        ),))
    scheduler = Scheduler(
        store=store, router=Router((AgentConfig("codex", 100),)),
        worker=ScriptedWorker({}), worktree_root=tmp_path / "worktrees",
        event_sources=(events,),
    )
    events.emit(Event(EventType.CAPACITY_PROBE, now, {"capacity": Capacity(
        agent="codex", state=CapacityState.UNKNOWN, observed_at=now,
        source="provider", confidence="low",
    )}))
    assert not scheduler.tick(now=now)
    if blocker is None:
        assert "codex" not in scheduler._cooldowns
        assert store.load_snapshot().capacities == ()
    else:
        assert scheduler._cooldowns["codex"].state is blocker
        assert store.load_snapshot().capacities[0].state is blocker


@pytest.mark.parametrize("payload", ["not-json", '{"type":"future.result","subtype":"success"}'])
def test_generic_unknown_keeps_failure_budget_behavior(tmp_path: Path, payload: str) -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    result = parse_claude_result(ClaudeProcessOutcome(exit_code=0, stdout=payload))
    assert result.kind is AgentResultKind.UNKNOWN
    worker = ScriptedWorker({(450, "claude"): (result,)})
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router((AgentConfig("claude", 100),)), worker=worker,
        worktree_root=tmp_path / "worktrees",
    )
    scheduler.discover((Issue(number=450, title="Unknown Claude result"),))
    assert scheduler.tick((Capacity(
        agent="claude", state=CapacityState.AVAILABLE, observed_at=now,
        source="provider", confidence="high",
    ),), now=now)
    task = scheduler.tasks[0]
    assert task.status is TaskState.READY
    assert task.attempt == 1
    assert task.per_agent_failures == (("claude", 1),)
    assert "claude" not in scheduler._cooldowns
