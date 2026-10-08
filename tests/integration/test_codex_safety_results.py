from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from subsched.agents.codex import parse_codex_jsonl
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
        ("capacity-reset-unknown.jsonl", CapacityState.UNKNOWN),
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
