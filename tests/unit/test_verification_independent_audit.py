from __future__ import annotations

from pathlib import Path

import pytest

from subsched.agents.base import ProcessExecutionRequest, ProcessExecutionResult
from subsched.agents.isolation import IsolationGitContext
from subsched.audit_prepare import AuditUnavailable
from subsched.config import IndependentAuditConfig, NativeIsolationConfig
from subsched.verification import run_verification


def config() -> IndependentAuditConfig:
    return IndependentAuditConfig(
        "owner/repo", 11, 22, "b" * 40, "c" * 64, 33, 44, "pip-audit 2.10.1", "test", 900
    )


@pytest.mark.parametrize("case", ["missing", "cleanup", "stale_after", "valid"])
def test_verification_requires_audit_before_and_after_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    import subsched.audit_prepare as audit
    import subsched.verification as verification

    calls: list[str] = []

    def git_snapshot(worktree: Path, root: Path) -> IsolationGitContext:
        root.mkdir()
        return IsolationGitContext(root, "a" * 40, root)

    def prepare(*args: object, **kwargs: object) -> Path:
        calls.append("audit")
        if case in {"missing", "cleanup"} or (case == "stale_after" and len(calls) == 3):
            raise AuditUnavailable(cleanup_confirmed=case != "cleanup")
        path = args[6]
        assert isinstance(path, Path)
        path.mkdir()
        return path

    def wrap(request: ProcessExecutionRequest, **kwargs: object) -> ProcessExecutionRequest:
        assert isinstance(kwargs.get("audit_cache"), Path)
        return request

    def run(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        calls.append("gate")
        return ProcessExecutionResult(exit_code=0, stdout="ok")

    monkeypatch.setattr(verification, "prepare_verification_git", git_snapshot)
    monkeypatch.setattr(audit, "prepare_independent_audit", prepare)
    monkeypatch.setattr(verification, "wrap_verification_request", wrap)
    monkeypatch.setattr(verification, "run_process_group", run)
    monkeypatch.setattr(verification, "cleanup_native_container", lambda *args, **kwargs: None)
    report = run_verification(
        tmp_path,
        ("true",),
        isolation_config=NativeIsolationConfig(
            backend="container", image="image@sha256:" + "a" * 64
        ),
        isolation_runtime_executable=Path("/usr/bin/true"),
        independent_audit=config(),
        audit_repository="owner/repo",
    )
    assert report.passed is (case == "valid")
    assert report.audit_unavailable is (case != "valid")
    assert report.cleanup_confirmed is (case != "cleanup")
    assert calls == (["audit"] if case in {"missing", "cleanup"} else ["audit", "gate", "audit"])


def test_scheduler_escalates_unavailable_audit_without_consuming_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    import subsched.verification as verification
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
    from subsched.verification import VerificationReport

    seen: list[object] = []

    def fake_verify(
        worktree: Path, commands: tuple[str, ...], **kwargs: object
    ) -> VerificationReport:
        seen.append(kwargs.get("independent_audit"))
        return VerificationReport(
            passed=False, gates=(), summary="audit unavailable", audit_unavailable=True
        )

    monkeypatch.setattr(verification, "run_verification", fake_verify)
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)}),
        worktree_root=tmp_path / "worktrees",
        independent_audit=config(),
        repo="owner/repo",
    )
    scheduler.discover([Issue(number=101, title="Task")])
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
    assert seen == [config()]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.needs_human_reason_code == "external_prerequisite"
    assert task.verification_failures == 0 and task.pr is None


def test_independent_audit_cannot_pass_without_a_gate(tmp_path: Path) -> None:
    report = run_verification(
        tmp_path, (),
        isolation_config=NativeIsolationConfig(
            backend="container", image="image@sha256:" + "a" * 64
        ),
        isolation_runtime_executable=Path("/usr/bin/true"),
        independent_audit=config(),
        audit_repository="owner/repo",
    )
    assert not report.passed
    assert not report.gates


def test_scheduler_audit_requires_a_task_worktree(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from subsched.models import AgentResult, AgentResultKind, Task, TaskState
    from subsched.router import AgentConfig, Router
    from subsched.scheduler import Scheduler, ScriptedWorker
    from subsched.storage import JsonStateStore

    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({}),
        worktree_root=tmp_path / "worktrees",
        independent_audit=config(),
        repo="owner/repo",
    )
    task = Task(task_id="github-101", issue_number=101, title="Task", labels=(),
                status=TaskState.IN_PROGRESS, worktree=None)
    scheduler.queue = scheduler.queue.append((task,))
    scheduler._handle_result(task, "claude", AgentResult(AgentResultKind.PASS), datetime.now(UTC))
    result = scheduler.queue.get(101)
    assert result is not None and result.status is TaskState.NEEDS_HUMAN
    assert result.needs_human_reason_code == "external_prerequisite"
