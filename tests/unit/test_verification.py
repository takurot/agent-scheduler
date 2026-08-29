from __future__ import annotations

import sys
from pathlib import Path

import pytest

from subsched.verification import run_verification


def test_run_verification_all_pass(tmp_path: Path) -> None:
    commands = (
        f"{sys.executable} -c \"print('gate1 ok')\"",
        f"{sys.executable} -c \"print('gate2 ok')\"",
    )
    report = run_verification(tmp_path, commands)
    assert report.passed is True
    assert len(report.gates) == 2
    assert report.gates[0].passed is True
    assert report.gates[1].passed is True
    assert "PASS" in report.summary


def test_run_verification_failure_stops_pipeline(tmp_path: Path) -> None:
    commands = (
        f'{sys.executable} -c "import sys; sys.exit(1)"',
        f"{sys.executable} -c \"print('should not run')\"",
    )
    report = run_verification(tmp_path, commands)
    assert report.passed is False
    assert len(report.gates) == 1
    assert report.gates[0].passed is False
    assert "FAIL" in report.summary


def test_run_verification_reports_command_not_found_clearly(tmp_path: Path) -> None:
    """Regression test for #129: a bare command that isn't on PATH (e.g. `pytest` in a
    uv-managed project without an activated venv) must produce a summary that clearly
    says the command was not found, not just a generic "FAIL (exit 1)" that is
    indistinguishable from a real gate failure."""
    commands = ("subsched-definitely-does-not-exist-abcxyz",)
    report = run_verification(tmp_path, commands)
    assert report.passed is False
    assert report.gates[0].command_not_found is True
    assert "command not found" in report.summary.casefold()


@pytest.mark.parametrize("commands", ((), ("   ", "\t")))
def test_run_verification_fails_when_no_executable_gate_runs(
    tmp_path: Path, commands: tuple[str, ...]
) -> None:
    report = run_verification(tmp_path, commands)

    assert report.passed is False
    assert report.gates == ()
    assert report.summary == "FAIL (no executable verification commands configured)"


def test_scheduler_does_not_finalize_when_no_verification_gate_runs(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from subsched.models import (
        AgentResult,
        AgentResultKind,
        Capacity,
        CapacityState,
        Issue,
        TaskState,
    )
    from subsched.router import AgentConfig, Router
    from subsched.scheduler import Scheduler, ScriptedWorker
    from subsched.storage import JsonStateStore

    store = JsonStateStore(tmp_path / "state.json")
    scheduler = Scheduler(
        store=store,
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker(
            {(101, "claude"): (AgentResult(AgentResultKind.PASS),)}
        ),
        worktree_root=tmp_path / "worktrees",
        verification_commands=(),
        max_verification_failures=1,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])

    scheduler.tick(
        [
            Capacity(
                agent="claude",
                state=CapacityState.AVAILABLE,
                observed_at=datetime.now(UTC),
                source="provider",
                confidence="high",
            )
        ]
    )

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.verification_failures == 1
    assert task.pr is None


def test_scheduler_passes_configured_verification_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verif_mod
    from subsched.models import AgentResult, AgentResultKind, Capacity, CapacityState, Issue
    from subsched.router import AgentConfig, Router
    from subsched.scheduler import Scheduler, ScriptedWorker
    from subsched.storage import JsonStateStore

    passed_commands: list[tuple[str, ...]] = []

    def mock_run_verification(
        worktree: Path, commands: tuple[str, ...], **_: object
    ) -> object:
        passed_commands.append(commands)
        from subsched.verification import VerificationReport

        return VerificationReport(passed=True, gates=(), summary="PASS")

    monkeypatch.setattr(verif_mod, "run_verification", mock_run_verification)

    store = JsonStateStore(tmp_path / "state.json")
    router = Router([AgentConfig("claude", priority=100)])
    worker = ScriptedWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)})

    custom_commands = ("echo 'custom-1'", "echo 'custom-2'")
    scheduler = Scheduler(
        store=store,
        router=router,
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        verification_commands=custom_commands,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])

    from datetime import UTC, datetime

    cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=datetime.now(UTC),
        source="provider",
        confidence="high",
    )
    scheduler.tick([cap])

    assert len(passed_commands) == 1
    assert passed_commands[0] == custom_commands


def test_scheduler_passes_configured_verification_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verif_mod
    from subsched.models import AgentResult, AgentResultKind, Capacity, CapacityState, Issue
    from subsched.router import AgentConfig, Router
    from subsched.scheduler import Scheduler, ScriptedWorker
    from subsched.storage import JsonStateStore

    seen_timeouts: list[float] = []

    def mock_run_verification(
        worktree: Path, commands: tuple[str, ...], **kwargs: object
    ) -> object:
        seen_timeouts.append(float(kwargs["timeout_seconds"]))  # type: ignore[arg-type]
        from subsched.verification import VerificationReport

        return VerificationReport(passed=True, gates=(), summary="PASS")

    monkeypatch.setattr(verif_mod, "run_verification", mock_run_verification)

    store = JsonStateStore(tmp_path / "state.json")
    router = Router([AgentConfig("claude", priority=100)])
    worker = ScriptedWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)})

    scheduler = Scheduler(
        store=store,
        router=router,
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        verification_timeout_seconds=45.0,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])

    from datetime import UTC, datetime

    cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=datetime.now(UTC),
        source="provider",
        confidence="high",
    )
    scheduler.tick([cap])

    assert seen_timeouts == [45.0]


def test_scheduler_rejects_non_positive_verification_timeout(tmp_path: Path) -> None:
    from subsched.router import AgentConfig, Router
    from subsched.scheduler import Scheduler, ScriptedWorker
    from subsched.storage import JsonStateStore

    with pytest.raises(ValueError, match="verification_timeout_seconds"):
        Scheduler(
            store=JsonStateStore(tmp_path / "state.json"),
            router=Router([AgentConfig("claude", priority=100)]),
            worker=ScriptedWorker({}),
            worktree_root=tmp_path / "worktrees",
            verification_timeout_seconds=0,
        )
