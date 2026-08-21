from __future__ import annotations

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
        if agent == "claude":
            req = ProcessExecutionRequest(
                argv=(
                    "claude",
                    "--print",
                    "--json-schema",
                    "result.schema.json",
                    "--output-format",
                    "json",
                    "--permission-mode",
                    "dontAsk",
                    "--safe-mode",
                    "--no-session-persistence",
                    "--strict-mcp-config",
                    "--tools",
                    "bash,edit,view",
                ),
                cwd=worktree_path,
                env={},
                stdin_payload=prompt.encode("utf-8"),
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
                env={},
                stdin_payload=prompt.encode("utf-8"),
            )
            return self.codex_agent.execute(req)
        return AgentResult(AgentResultKind.FAILURE, output=f"unsupported agent: {agent}")
