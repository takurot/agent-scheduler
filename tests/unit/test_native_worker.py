from __future__ import annotations

from pathlib import Path

from subsched.agents.native import NativeWorker
from subsched.contract import bootstrap_task_files
from subsched.models import AgentResultKind, Issue, Task


def test_native_worker_fails_if_preconditions_missing(tmp_path: Path) -> None:
    worker = NativeWorker()
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    result = worker.run(task, "claude")
    assert result.kind is AgentResultKind.FAILURE
    assert "dispatch preconditions failed" in result.output


def test_native_worker_unsupported_agent(tmp_path: Path) -> None:
    worker = NativeWorker()
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)
    result = worker.run(task, "unknown_agent")
    assert result.kind is AgentResultKind.FAILURE
    assert "unsupported agent" in result.output
