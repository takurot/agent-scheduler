from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from subsched.agents.base import ProcessExecutionRequest
from subsched.agents.claude import ClaudeAgent, ClaudeBillingMode, ClaudeExecutionPolicy
from subsched.agents.codex import CodexAgent
from subsched.contract import build_worker_prompt, validate_dispatch_preconditions
from subsched.models import AgentResult, AgentResultKind, Task


class NativeWorker:
    """Dispatches tasks to native agent adapters (ClaudeAgent / CodexAgent)."""

    def __init__(
        self,
        claude_agent: ClaudeAgent | None = None,
        codex_agent: CodexAgent | None = None,
        agent_timeout_seconds: float = 300.0,
        # #139: the same verification.commands tuple the Scheduler's post-worker gate
        # re-runs, injected here as trusted config (not derived from Issue body/handoff)
        # so the agent and the gate can never diverge on what "verification passes"
        # means. Defaults to () for backward compatibility -- build_worker_prompt already
        # falls back to a generic "see docs/WORKFLOW.md or pyproject.toml" line when empty.
        verification_commands: Sequence[str] = (),
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
        self.verification_commands = tuple(verification_commands)

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

        prompt = build_worker_prompt(task, verification_commands=self.verification_commands)
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
            )
            return self.codex_agent.execute(req)
        return AgentResult(AgentResultKind.FAILURE, output=f"unsupported agent: {agent}")
