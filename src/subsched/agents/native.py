from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path

from subsched.agents.base import ProcessExecutionRequest
from subsched.agents.claude import ClaudeAgent, ClaudeBillingMode, ClaudeExecutionPolicy
from subsched.agents.codex import (
    CodexAgent,
    CodexApprovalMode,
    build_codex_headless_argv,
    ensure_codex_output_schema,
)
from subsched.contract import (
    build_plan_prompt,
    build_plan_review_prompt,
    build_review_prompt,
    build_revision_prompt,
    build_worker_prompt,
    validate_dispatch_preconditions,
)
from subsched.models import AgentResult, AgentResultKind, Task, TaskState
from subsched.plan_review import READ_ONLY_SANDBOX_ARGS
from subsched.structured_logger import StructuredLogger

# #141: default heartbeat cadence for a long-running agent invocation. Kept as a module
# constant (rather than hardcoded inline) so tests can reference the same value.
HEARTBEAT_INTERVAL_SECONDS = 60.0


class NativeWorker:
    """Dispatches tasks to native agent adapters (ClaudeAgent / CodexAgent)."""

    def __init__(
        self,
        claude_agent: ClaudeAgent | None = None,
        codex_agent: CodexAgent | None = None,
        agent_timeout_seconds: float = 300.0,
        # #141: optional -- when configured, a "heartbeat" event is logged roughly every
        # HEARTBEAT_INTERVAL_SECONDS while a claude/codex subprocess is still running, so
        # `status --verbose`/JSONL observers can tell "still running" from "hung/stalled"
        # during the long synchronous worker.run() call. None (default) disables it
        # entirely -- backward compatible with every existing NativeWorker() call site.
        structured_logger: StructuredLogger | None = None,
        # #139: the same verification.commands tuple the Scheduler's post-worker gate
        # re-runs, injected here as trusted config (not derived from Issue body/handoff)
        # so the agent and the gate can never diverge on what "verification passes"
        # means. Defaults to () for backward compatibility -- build_worker_prompt already
        # falls back to a generic "see docs/WORKFLOW.md or pyproject.toml" line when empty.
        verification_commands: Sequence[str] = (),
        subscription_billing_verified: bool = False,
        codex_output_schema: Path | None = None,
        # #291: the approval-flag variant a `parse_codex_cli_metadata()` capability
        # check selected for the installed Codex CLI's `exec` surface. Defaults to the
        # variant currently confirmed live; callers that already ran preflight (e.g.
        # `subsched run --allow-native`) must pass through its actual detected mode so
        # NativeWorker never diverges from what preflight verified was safe.
        codex_approval_mode: CodexApprovalMode = CodexApprovalMode.APPROVE_FOR_ME,
    ) -> None:
        if type(subscription_billing_verified) is not bool:
            raise TypeError("subscription billing verification must be a boolean")
        billing_mode = (
            ClaudeBillingMode.SUBSCRIPTION_VERIFIED
            if subscription_billing_verified
            else ClaudeBillingMode.UNKNOWN
        )
        self.claude_agent = claude_agent or ClaudeAgent(
            ClaudeExecutionPolicy(
                live_probe_opt_in=True,
                billing_mode=billing_mode,
            )
        )
        self.codex_agent = codex_agent or CodexAgent(
            allow_live=True,
            subscription_billing_verified=subscription_billing_verified,
        )
        self.agent_timeout_seconds = agent_timeout_seconds
        self.structured_logger = structured_logger
        self.verification_commands = tuple(verification_commands)
        self.codex_output_schema = codex_output_schema
        self.codex_approval_mode = codex_approval_mode

    def _heartbeat(self, task: Task, agent: str) -> Callable[[float], None] | None:
        logger = self.structured_logger
        if logger is None:
            return None

        def _emit(elapsed_seconds: float) -> None:
            logger.log(
                "heartbeat",
                issue_number=task.issue_number,
                agent=agent,
                task_id=task.task_id,
                data={"elapsed_seconds": round(elapsed_seconds, 1)},
            )

        return _emit

    def run(self, task: Task, agent: str) -> AgentResult:
        if task.worktree is None:
            return AgentResult(AgentResultKind.FAILURE, output="missing task worktree")
        worktree_path = Path(task.worktree)
        try:
            validate_dispatch_preconditions(worktree_path, task)
        except Exception as e:
            return AgentResult(
                AgentResultKind.FAILURE,
                output=f"dispatch preconditions failed: {e}",
            )

        # Multi-stage workflow prompt and sandbox routing:
        # #280: PLANNING and PLAN_REVIEW are the pre-implementation gate.
        # #281: PR_REVIEW is the post-PR evaluation gate, and REVISING re-dispatches the worker.
        if task.status is TaskState.PLANNING:
            prompt = build_plan_prompt(task)
            read_only = False
        elif task.status is TaskState.PLAN_REVIEW:
            prompt = build_plan_review_prompt(task)
            read_only = True
        elif task.dispatch_status is TaskState.PR_REVIEW:
            prompt = build_review_prompt(task, round_number=task.review_cycles + 1)
            read_only = True
        elif task.dispatch_status is TaskState.REVISING:
            prompt = build_revision_prompt(task, verification_commands=self.verification_commands)
            read_only = False
        else:
            prompt = build_worker_prompt(task, verification_commands=self.verification_commands)
            read_only = False
        heartbeat = self._heartbeat(task, agent)
        if agent == "claude":
            claude_tools = READ_ONLY_SANDBOX_ARGS["claude"][1] if read_only else "Bash,Edit,Read"
            req = ProcessExecutionRequest(
                argv=(
                    "claude",
                    "--print",
                    "--output-format",
                    "json",
                    # bypassPermissions: --print is non-interactive, so any mode that can
                    # prompt (including "dontAsk", which denies rather than auto-approves
                    # when there is no one to ask) blocks every tool call and the agent can
                    # never actually do anything. The task's isolated git worktree plus
                    # mandatory PR review before merge are the safety boundary here, not
                    # per-command approval.
                    "--permission-mode",
                    "bypassPermissions",
                    "--no-session-persistence",
                    "--strict-mcp-config",
                    "--tools",
                    claude_tools,
                ),
                cwd=worktree_path,
                # ClaudeAgent/CodexAgent.execute() apply COMMON_ENV_ALLOWLIST to this before
                # launching the subprocess, so secrets in the parent environment are not
                # passed through. But subprocess.Popen(argv, env=...) treats an explicit env
                # as *replacing* the child's environment entirely (not inheriting the
                # parent's) -- so this must start from a real environment (with PATH, HOME,
                # etc.) for the allowlist filtering to have anything useful left to keep, or
                # a bare command name like "claude" can never be resolved (see #120).
                env=dict(os.environ),
                stdin_payload=prompt.encode("utf-8"),
                timeout_seconds=self.agent_timeout_seconds,
                heartbeat=heartbeat,
                heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
            )
            return self.claude_agent.execute(req)
        elif agent == "codex":
            schema_path = self.codex_output_schema or (
                worktree_path / ".ai" / "codex-output.schema.json"
            )
            ensure_codex_output_schema(schema_path)
            codex_sandbox = (
                READ_ONLY_SANDBOX_ARGS["codex"][1] if read_only else "workspace-write"
            )
            req = ProcessExecutionRequest(
                argv=build_codex_headless_argv(
                    executable="codex",
                    approval_mode=self.codex_approval_mode,
                    sandbox=codex_sandbox,
                    output_schema=schema_path,
                    cwd=worktree_path,
                ),
                cwd=worktree_path,
                # ClaudeAgent/CodexAgent.execute() apply COMMON_ENV_ALLOWLIST to this before
                # launching the subprocess, so secrets in the parent environment are not
                # passed through. But subprocess.Popen(argv, env=...) treats an explicit env
                # as *replacing* the child's environment entirely (not inheriting the
                # parent's) -- so this must start from a real environment (with PATH, HOME,
                # etc.) for the allowlist filtering to have anything useful left to keep, or
                # a bare command name like "claude" can never be resolved (see #120).
                env=dict(os.environ),
                stdin_payload=prompt.encode("utf-8"),
                timeout_seconds=self.agent_timeout_seconds,
                heartbeat=heartbeat,
                heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
            )
            return self.codex_agent.execute(req)
        return AgentResult(AgentResultKind.FAILURE, output=f"unsupported agent: {agent}")
