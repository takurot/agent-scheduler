from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from subsched.agents.base import ProcessExecutionRequest
from subsched.agents.claude import ClaudeAgent, ClaudeBillingMode, ClaudeExecutionPolicy
from subsched.agents.codex import CodexAgent
from subsched.contract import build_worker_prompt, validate_dispatch_preconditions
from subsched.models import AgentResult, AgentResultKind, Task
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
    ) -> None:
        self.claude_agent = claude_agent or ClaudeAgent(
            ClaudeExecutionPolicy(
                live_probe_opt_in=True,
                billing_mode=ClaudeBillingMode.SUBSCRIPTION_VERIFIED,
            )
        )
        self.codex_agent = codex_agent or CodexAgent(
            allow_live=True,
            subscription_billing_verified=True,
        )
        self.agent_timeout_seconds = agent_timeout_seconds
        self.structured_logger = structured_logger

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

        prompt = build_worker_prompt(task)
        heartbeat = self._heartbeat(task, agent)
        if agent == "claude":
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
                    "Bash,Edit,Read",
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
            req = ProcessExecutionRequest(
                argv=(
                    "codex",
                    "--ask-for-approval",
                    "never",
                    "exec",
                    "--strict-config",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--json",
                    "--sandbox",
                    "workspace-write",
                    "--ephemeral",
                    "-",
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
