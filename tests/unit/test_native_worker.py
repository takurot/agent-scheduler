from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from subsched.agents.native import NativeWorker
from subsched.contract import bootstrap_task_files
from subsched.models import AgentResult, AgentResultKind, Issue, Task


def test_native_worker_missing_worktree() -> None:
    worker = NativeWorker()
    task = Task.from_issue(Issue(number=101, title="Test"))
    result = worker.run(task, "claude")
    assert result.kind is AgentResultKind.FAILURE
    assert "missing task worktree" in result.output


def test_native_worker_fails_if_preconditions_missing(tmp_path: Path) -> None:
    worker = NativeWorker()
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    result = worker.run(task, "claude")
    assert result.kind is AgentResultKind.FAILURE
    assert "dispatch preconditions failed" in result.output


def test_native_worker_dispatches_to_claude_and_codex(tmp_path: Path) -> None:
    mock_claude = MagicMock()
    mock_claude.execute.return_value = AgentResult(AgentResultKind.PASS)
    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(claude_agent=mock_claude, codex_agent=mock_codex)
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    res_claude = worker.run(task, "claude")
    assert res_claude.kind is AgentResultKind.PASS
    assert mock_claude.execute.called

    res_codex = worker.run(task, "codex")
    assert res_codex.kind is AgentResultKind.PASS
    assert mock_codex.execute.called


def test_native_worker_unsupported_agent(tmp_path: Path) -> None:
    worker = NativeWorker()
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)
    result = worker.run(task, "unknown_agent")
    assert result.kind is AgentResultKind.FAILURE
    assert "unsupported agent" in result.output
