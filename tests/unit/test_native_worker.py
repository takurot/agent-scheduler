from __future__ import annotations

import os
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


def test_native_worker_passes_a_resolvable_path_to_claude_and_codex(tmp_path: Path) -> None:
    """Regression test for #120: NativeWorker previously built its ProcessExecutionRequest
    with env={}, which subprocess.Popen treats as *replacing* the child's environment
    entirely -- so a bare command name like "claude" could never be resolved via PATH and
    every native dispatch failed with FileNotFoundError before the agent ever ran."""
    mock_claude = MagicMock()
    mock_claude.execute.return_value = AgentResult(AgentResultKind.PASS)
    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(claude_agent=mock_claude, codex_agent=mock_codex)
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "claude")
    claude_request = mock_claude.execute.call_args[0][0]
    assert claude_request.env.get("PATH") == os.environ.get("PATH")

    worker.run(task, "codex")
    codex_request = mock_codex.execute.call_args[0][0]
    assert codex_request.env.get("PATH") == os.environ.get("PATH")


def test_native_worker_claude_argv_uses_flags_the_real_cli_accepts(tmp_path: Path) -> None:
    """Regression test for the argv-level bugs found dogfooding #120 against real claude
    2.1.245: `--safe-mode` does not exist, `--json-schema result.schema.json` is not valid
    JSON (it's a filename, not schema content), `--tools bash,edit,view` uses casing/names
    the CLI doesn't recognize (silently disabling every tool), and `--permission-mode
    dontAsk` denies every action in --print (non-interactive) mode -- so autonomous
    execution could never actually do anything even once it launched successfully."""
    mock_claude = MagicMock()
    mock_claude.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(claude_agent=mock_claude, codex_agent=MagicMock())
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "claude")
    argv = mock_claude.execute.call_args[0][0].argv

    assert "--safe-mode" not in argv
    assert "--json-schema" not in argv
    assert "result.schema.json" not in argv
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--tools" in argv
    tools = argv[argv.index("--tools") + 1]
    assert set(tools.split(",")) == {"Bash", "Edit", "Read"}


def test_native_worker_unsupported_agent(tmp_path: Path) -> None:
    worker = NativeWorker()
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)
    result = worker.run(task, "unknown_agent")
    assert result.kind is AgentResultKind.FAILURE
    assert "unsupported agent" in result.output
