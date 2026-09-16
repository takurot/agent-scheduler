from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from subsched.agents.claude import ClaudeBillingMode
from subsched.agents.codex import CodexApprovalMode
from subsched.agents.native import NativeWorker
from subsched.contract import bootstrap_task_files
from subsched.models import AgentResult, AgentResultKind, Issue, Task, TaskState


@pytest.fixture(autouse=True)
def _admit_legacy_request_shape_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep pre-#293 request-shape tests focused below the isolation boundary."""
    monkeypatch.setattr("subsched.agents.native.native_isolation_failure", lambda: None)


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
        worker.claude_agent.execution_policy.billing_mode is ClaudeBillingMode.SUBSCRIPTION_VERIFIED
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

    worker = NativeWorker(
        claude_agent=mock_claude,
        codex_agent=mock_codex,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
    )
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

    worker = NativeWorker(
        claude_agent=mock_claude,
        codex_agent=mock_codex,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
    )
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


def test_native_worker_omits_model_flag_when_no_policy_configured(tmp_path: Path) -> None:
    mock_claude = MagicMock()
    mock_claude.execute.return_value = AgentResult(AgentResultKind.PASS)
    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(
        claude_agent=mock_claude,
        codex_agent=mock_codex,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
    )
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "claude")
    assert "--model" not in mock_claude.execute.call_args[0][0].argv

    worker.run(task, "codex")
    assert "--model" not in mock_codex.execute.call_args[0][0].argv


def test_native_worker_resolves_model_per_execution_stage(tmp_path: Path) -> None:
    """#296: planning/plan_review/pr_review/revision use their configured stage model;
    an ordinary implementation dispatch falls back to `default`."""
    from dataclasses import replace

    from subsched.config import AgentModelPolicy, AgentSettings
    from subsched.models import TaskState

    mock_claude = MagicMock()
    mock_claude.execute.return_value = AgentResult(AgentResultKind.PASS)
    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(
        claude_agent=mock_claude,
        codex_agent=mock_codex,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
        agents={
            "claude": AgentSettings(
                models=AgentModelPolicy(
                    default="sonnet",
                    planning="opus",
                    plan_review="opus",
                    pr_review="opus",
                    revision="sonnet",
                )
            ),
            "codex": AgentSettings(
                models=AgentModelPolicy(default="standard-model", planning="advanced-model")
            ),
        },
    )
    base = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, base)

    def claude_model_for(task: Task) -> str | None:
        worker.run(task, "claude")
        argv = mock_claude.execute.call_args[0][0].argv
        return argv[argv.index("--model") + 1] if "--model" in argv else None

    def codex_model_for(task: Task) -> str | None:
        worker.run(task, "codex")
        argv = mock_codex.execute.call_args[0][0].argv
        return argv[argv.index("--model") + 1] if "--model" in argv else None

    planning_task = replace(base, status=TaskState.PLANNING)
    assert claude_model_for(planning_task) == "opus"
    assert codex_model_for(planning_task) == "advanced-model"

    plan_review_task = replace(base, status=TaskState.PLAN_REVIEW)
    assert claude_model_for(plan_review_task) == "opus"

    implementation_task = base
    assert claude_model_for(implementation_task) == "sonnet"
    assert codex_model_for(implementation_task) == "standard-model"

    pr_review_task = replace(base, dispatch_status=TaskState.PR_REVIEW)
    assert claude_model_for(pr_review_task) == "opus"

    revising_task = replace(base, dispatch_status=TaskState.REVISING)
    assert claude_model_for(revising_task) == "sonnet"


def test_native_worker_omits_effort_flag_when_no_policy_configured(tmp_path: Path) -> None:
    mock_claude = MagicMock()
    mock_claude.execute.return_value = AgentResult(AgentResultKind.PASS)
    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(
        claude_agent=mock_claude,
        codex_agent=mock_codex,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
    )
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "claude")
    assert "--effort" not in mock_claude.execute.call_args[0][0].argv

    worker.run(task, "codex")
    assert not any(
        arg.startswith("model_reasoning_effort=") for arg in mock_codex.execute.call_args[0][0].argv
    )


def test_native_worker_resolves_effort_per_execution_stage(tmp_path: Path) -> None:
    """#313: planning/plan_review/pr_review/revision use their configured stage effort;
    an ordinary implementation dispatch falls back to `default`."""
    from dataclasses import replace

    from subsched.config import AgentEffortPolicy, AgentSettings
    from subsched.models import TaskState

    mock_claude = MagicMock()
    mock_claude.execute.return_value = AgentResult(AgentResultKind.PASS)
    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(
        claude_agent=mock_claude,
        codex_agent=mock_codex,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
        agents={
            "claude": AgentSettings(
                effort=AgentEffortPolicy(
                    default="medium",
                    planning="high",
                    plan_review="high",
                    pr_review="high",
                    revision="medium",
                )
            ),
            "codex": AgentSettings(effort=AgentEffortPolicy(default="low", planning="high")),
        },
    )
    base = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, base)

    def claude_effort_for(task: Task) -> str | None:
        worker.run(task, "claude")
        argv = mock_claude.execute.call_args[0][0].argv
        return argv[argv.index("--effort") + 1] if "--effort" in argv else None

    def codex_effort_for(task: Task) -> str | None:
        worker.run(task, "codex")
        argv = mock_codex.execute.call_args[0][0].argv
        for i, arg in enumerate(argv):
            if (
                arg == "-c"
                and i + 1 < len(argv)
                and argv[i + 1].startswith("model_reasoning_effort=")
            ):
                val = argv[i + 1].split("=", 1)[1]
                return val.strip('"')
        return None

    planning_task = replace(base, status=TaskState.PLANNING)
    assert claude_effort_for(planning_task) == "high"
    assert codex_effort_for(planning_task) == "high"

    plan_review_task = replace(base, status=TaskState.PLAN_REVIEW)
    assert claude_effort_for(plan_review_task) == "high"

    implementation_task = base
    assert claude_effort_for(implementation_task) == "medium"
    assert codex_effort_for(implementation_task) == "low"

    pr_review_task = replace(base, dispatch_status=TaskState.PR_REVIEW)
    assert claude_effort_for(pr_review_task) == "high"

    revising_task = replace(base, dispatch_status=TaskState.REVISING)
    assert claude_effort_for(revising_task) == "medium"


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

    worker = NativeWorker(
        claude_agent=mock_claude,
        codex_agent=mock_codex,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
    )
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
    assert "Do not reset, clean, overwrite, or delete existing dirty worktree changes." in prompt


def test_native_worker_includes_configured_verification_commands_in_codex_prompt(
    tmp_path: Path,
) -> None:
    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(
        claude_agent=MagicMock(),
        codex_agent=mock_codex,
        verification_commands=("uv run mypy src",),
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
    )
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "codex")
    stdin_payload = mock_codex.execute.call_args[0][0].stdin_payload
    prompt = stdin_payload.decode("utf-8")

    assert "uv run mypy src" in prompt
    assert "Do not reset, clean, overwrite, or delete existing dirty worktree changes." in prompt


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
        claude_agent=mock_claude,
        codex_agent=mock_codex,
        agent_timeout_seconds=900.0,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
    )
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "claude")
    assert mock_claude.execute.call_args[0][0].timeout_seconds == 900.0

    worker.run(task, "codex")
    assert mock_codex.execute.call_args[0][0].timeout_seconds == 900.0


def test_native_worker_codex_requests_output_schema_and_mentions_schema_in_prompt(
    tmp_path: Path,
) -> None:
    """#205: NativeWorker executing Codex must supply --output-schema in argv with a valid
    schema file and include the final result schema contract in the prompt."""
    import json

    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(
        codex_agent=mock_codex,
        subscription_billing_verified=True,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
    )
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "codex")

    req = mock_codex.execute.call_args[0][0]
    assert "--output-schema" in req.argv
    schema_idx = req.argv.index("--output-schema")
    schema_path = Path(req.argv[schema_idx + 1])
    assert schema_path.is_file()

    schema_data = json.loads(schema_path.read_text(encoding="utf-8"))
    assert set(schema_data.get("required", [])) == {"result", "summary", "reason_code"}

    prompt = req.stdin_payload.decode("utf-8")
    assert '{"result"' in prompt


def test_native_worker_codex_accepts_configured_output_schema(tmp_path: Path) -> None:
    """#205: Configured codex_output_schema is passed to Codex argv."""
    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    custom_schema = tmp_path / "custom.schema.json"
    custom_schema.write_text('{"type": "object"}', encoding="utf-8")

    worker = NativeWorker(
        codex_agent=mock_codex,
        codex_output_schema=custom_schema,
        subscription_billing_verified=True,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
    )
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "codex")

    req = mock_codex.execute.call_args[0][0]
    schema_idx = req.argv.index("--output-schema")
    assert req.argv[schema_idx + 1] == str(custom_schema)


def test_native_worker_codex_refuses_missing_preflight_approval_mode(tmp_path: Path) -> None:
    """A Codex dispatch without a capability-selected mode must fail closed."""
    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(codex_agent=mock_codex, subscription_billing_verified=True)
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    result = worker.run(task, "codex")

    assert result.kind is AgentResultKind.FAILURE
    assert result.output == "missing preflight-detected Codex approval mode"
    mock_codex.execute.assert_not_called()


def test_native_worker_codex_uses_preflight_detected_legacy_approval_mode(
    tmp_path: Path,
) -> None:
    """A Codex CLI that only offers the legacy `--ask-for-approval` flag must still be
    dispatched with that flag when preflight detected it, not the newer one."""
    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(
        codex_agent=mock_codex,
        subscription_billing_verified=True,
        codex_approval_mode=CodexApprovalMode.ASK_FOR_APPROVAL_NEVER,
    )
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "codex")

    argv = mock_codex.execute.call_args[0][0].argv

    assert "--ask-for-approval" in argv
    assert argv[argv.index("--ask-for-approval") + 1] == "never"
    assert "--approve-for-me" not in argv


def test_native_worker_plan_review_codex_uses_plan_review_schema(tmp_path: Path) -> None:
    """#306: NativeWorker during PLAN_REVIEW passes plan_review=True and uses plan review schema."""
    import json
    from dataclasses import replace

    mock_codex = MagicMock()
    mock_codex.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(
        codex_agent=mock_codex,
        subscription_billing_verified=True,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
    )
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    task = replace(task, status=TaskState.PLAN_REVIEW)
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "codex")

    req = mock_codex.execute.call_args[0][0]
    assert req.plan_review is True
    assert "--output-schema" in req.argv
    schema_idx = req.argv.index("--output-schema")
    schema_path = Path(req.argv[schema_idx + 1])
    assert schema_path.name == "plan-review-output.schema.json"
    assert schema_path.is_file()

    schema_data = json.loads(schema_path.read_text(encoding="utf-8"))
    assert set(schema_data.get("required", [])) == {"verdict", "summary", "findings"}


def test_native_worker_plan_review_claude_passes_flag(tmp_path: Path) -> None:
    """#306: NativeWorker during PLAN_REVIEW passes plan_review=True to Claude."""
    from dataclasses import replace

    mock_claude = MagicMock()
    mock_claude.execute.return_value = AgentResult(AgentResultKind.PASS)

    worker = NativeWorker(
        claude_agent=mock_claude,
        subscription_billing_verified=True,
    )
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(tmp_path))
    task = replace(task, status=TaskState.PLAN_REVIEW)
    bootstrap_task_files(tmp_path, task)

    worker.run(task, "claude")

    req = mock_claude.execute.call_args[0][0]
    assert req.plan_review is True
