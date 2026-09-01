from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from subsched.agents.claude import ClaudeBillingMode
from subsched.agents.native import NativeWorker
from subsched.contract import bootstrap_task_files
from subsched.models import AgentResult, AgentResultKind, Issue, Task


def test_native_worker_missing_worktree() -> None:
    worker = NativeWorker()
    task = Task.from_issue(Issue(number=101, title="Test"))
    result = worker.run(task, "claude")
    assert result.kind is AgentResultKind.FAILURE
    assert "missing task worktree" in result.output


def test_native_worker_defaults_billing_to_unverified() -> None:
    worker = NativeWorker()

    assert worker.claude_agent.execution_policy.billing_mode is ClaudeBillingMode.UNKNOWN
    assert worker.codex_agent.subscription_billing_verified is False


def test_native_worker_accepts_explicit_subscription_billing_verification() -> None:
    worker = NativeWorker(subscription_billing_verified=True)

    assert (
        worker.claude_agent.execution_policy.billing_mode
        is ClaudeBillingMode.SUBSCRIPTION_VERIFIED
    )
    assert worker.codex_agent.subscription_billing_verified is True


def test_native_worker_rejects_non_boolean_billing_verification() -> None:
    with pytest.raises(TypeError, match="must be a boolean"):
        NativeWorker(subscription_billing_verified="false")  # type: ignore[arg-type]


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


def test_native_worker_defaults_agent_timeout_to_300_seconds(tmp_path: Path) -> None:
    """Regression test for #123: the agent execution timeout was hardcoded via the
    ProcessExecutionRequest dataclass default (300s) instead of being an explicit,
    configurable NativeWorker setting."""
    mock_claude = MagicMock()
    mock_claude.execute.return_value = AgentResult(AgentResultKind.PASS)
    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(claude_agent=mock_claude, codex_agent=mock_codex)
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "claude")
    assert mock_claude.execute.call_args[0][0].timeout_seconds == 300.0

    worker.run(task, "codex")
    assert mock_codex.execute.call_args[0][0].timeout_seconds == 300.0


def test_native_worker_wires_heartbeat_when_structured_logger_configured(
    tmp_path: Path,
) -> None:
    """Regression test for #141: when a structured_logger is configured, NativeWorker
    must pass a heartbeat callback into the ProcessExecutionRequest so run_process_group
    can emit progress during a long agent invocation. Without a logger (default), no
    callback is wired -- backward compatible with every existing call site."""
    from subsched.structured_logger import StructuredLogger

    mock_claude = MagicMock()
    mock_claude.execute.return_value = AgentResult(AgentResultKind.PASS)
    logged: list[dict[str, object]] = []

    class _RecordingLogger(StructuredLogger):
        def __init__(self) -> None:
            pass

        def log(self, event: str, **kwargs: object) -> dict[str, object]:
            entry = {"event": event, **kwargs}
            logged.append(entry)
            return entry

    worker = NativeWorker(
        claude_agent=mock_claude, codex_agent=MagicMock(), structured_logger=_RecordingLogger()
    )
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "claude")
    req = mock_claude.execute.call_args[0][0]
    assert req.heartbeat is not None

    req.heartbeat(12.3)
    assert logged
    assert logged[0]["event"] == "heartbeat"
    assert logged[0]["issue_number"] == 101
    assert logged[0]["agent"] == "claude"


def test_native_worker_no_heartbeat_when_no_structured_logger(tmp_path: Path) -> None:
    mock_claude = MagicMock()
    mock_claude.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(claude_agent=mock_claude, codex_agent=MagicMock())
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "claude")
    req = mock_claude.execute.call_args[0][0]
    assert req.heartbeat is None


def test_native_worker_includes_configured_verification_commands_in_claude_prompt(
    tmp_path: Path,
) -> None:
    """Regression test for #139: verification.commands was used by the Scheduler's
    post-worker gate but never passed into the worker prompt, so Claude/Codex had no way
    to know which commands actually define 'verification passes' -- it could only guess
    from docs/WORKFLOW.md or pyproject.toml, which may not match the configured gate."""
    mock_claude = MagicMock()
    mock_claude.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(
        claude_agent=mock_claude,
        codex_agent=MagicMock(),
        verification_commands=("uv run ruff check .", "uv run pytest -q"),
    )
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "claude")
    stdin_payload = mock_claude.execute.call_args[0][0].stdin_payload
    prompt = stdin_payload.decode("utf-8")

    assert "uv run ruff check ." in prompt
    assert "uv run pytest -q" in prompt
    assert (
        "Do not reset, clean, overwrite, or delete existing dirty worktree changes."
        in prompt
    )


def test_native_worker_includes_configured_verification_commands_in_codex_prompt(
    tmp_path: Path,
) -> None:
    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(
        claude_agent=MagicMock(),
        codex_agent=mock_codex,
        verification_commands=("uv run mypy src",),
    )
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "codex")
    stdin_payload = mock_codex.execute.call_args[0][0].stdin_payload
    prompt = stdin_payload.decode("utf-8")

    assert "uv run mypy src" in prompt
    assert (
        "Do not reset, clean, overwrite, or delete existing dirty worktree changes."
        in prompt
    )


def test_native_worker_defaults_to_no_verification_commands(tmp_path: Path) -> None:
    """Backward-compatible default: NativeWorker() with no configured commands must not
    break -- the prompt falls back to build_worker_prompt's existing generic guidance."""
    mock_claude = MagicMock()
    mock_claude.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(claude_agent=mock_claude, codex_agent=MagicMock())
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "claude")
    prompt = mock_claude.execute.call_args[0][0].stdin_payload.decode("utf-8")
    assert "defined in docs/WORKFLOW.md or pyproject.toml" in prompt


def test_native_worker_applies_configured_agent_timeout(tmp_path: Path) -> None:
    """#123: NativeWorker's agent execution timeout must be configurable so it can be
    raised above the 300s default for real issues that take longer to implement."""
    mock_claude = MagicMock()
    mock_claude.execute.return_value = AgentResult(AgentResultKind.PASS)
    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(
        claude_agent=mock_claude, codex_agent=mock_codex, agent_timeout_seconds=900.0
    )
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "claude")
    assert mock_claude.execute.call_args[0][0].timeout_seconds == 900.0

    worker.run(task, "codex")
    assert mock_codex.execute.call_args[0][0].timeout_seconds == 900.0
