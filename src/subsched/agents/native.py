from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from subsched.agents.base import ProcessExecutionRequest
from subsched.agents.claude import ClaudeAgent, ClaudeBillingMode, ClaudeExecutionPolicy
from subsched.agents.codex import (
    CodexAgent,
    CodexApprovalMode,
    build_codex_headless_argv,
    ensure_codex_output_schema,
    resolve_codex_sandbox_mode,
)
from subsched.agents.isolation import (
    IsolationGitContext,
    cleanup_native_container,
    import_isolated_git,
    native_container_name,
    native_isolation_failure,
    prepare_isolated_git,
    verify_native_isolation,
    wrap_native_request,
)
from subsched.config import AgentSettings, NativeIsolationConfig
from subsched.contract import (
    build_plan_prompt,
    build_plan_review_prompt,
    build_review_prompt,
    build_revision_prompt,
    build_worker_prompt,
    validate_dispatch_preconditions,
)
from subsched.models import AgentResult, AgentResultKind, Task, TaskState, resolve_stage
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
        # check selected for the installed Codex CLI's `exec` surface. None keeps
        # Claude-only construction possible, but every Codex dispatch fails closed
        # unless its caller passed through an actual preflight result.
        codex_approval_mode: CodexApprovalMode | None = None,
        isolation_config: NativeIsolationConfig | None = None,
        isolation_runtime_executable: Path | None = None,
        isolation_state_root: Path | None = None,
        # #296: per-agent stage model policy (config `agents.<provider>.models`).
        # Defaults to {} for backward compatibility -- every existing NativeWorker()
        # call site without an explicit models config resolves no model for any stage,
        # so the provider CLI's own default is used and argv is unchanged.
        agents: Mapping[str, AgentSettings] | None = None,
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
        self.isolation_config = isolation_config
        self.isolation_runtime_executable = isolation_runtime_executable
        self.isolation_state_root = isolation_state_root
        self.agents = agents or {}

    def _execute_isolated(
        self,
        request: ProcessExecutionRequest,
        *,
        agent: str,
        read_only: bool,
        git_context: IsolationGitContext,
    ) -> AgentResult:
        assert self.isolation_config is not None
        assert self.isolation_runtime_executable is not None
        try:
            container_name = native_container_name(git_context.git_dir.parent.parent.name)
        except ValueError as error:
            return AgentResult(AgentResultKind.FAILURE, output=str(error))
        try:
            isolated_request = wrap_native_request(
                request,
                agent=agent,
                config=self.isolation_config,
                runtime_executable=self.isolation_runtime_executable,
                git_dir=git_context.git_dir,
                worktree_git_mount=git_context.worktree_git_mount,
                read_only=read_only,
                container_name=container_name,
            )
        except ValueError as error:
            return AgentResult(AgentResultKind.FAILURE, output=str(error))
        adapter = self.claude_agent if agent == "claude" else self.codex_agent
        execution_failed = False
        try:
            result = adapter.execute(isolated_request)
        except Exception:
            execution_failed = True
            result = AgentResult(
                AgentResultKind.FAILURE,
                output="native agent execution failed before returning a result",
            )
        finally:
            cleanup_failure = cleanup_native_container(
                self.isolation_runtime_executable,
                container_name,
                env=isolated_request.env,
            )
        if cleanup_failure is not None:
            return AgentResult(AgentResultKind.FAILURE, output=cleanup_failure)
        import_failure = import_isolated_git(
            request.cwd, git_context, read_only=read_only
        )
        if import_failure is not None:
            return AgentResult(AgentResultKind.FAILURE, output=import_failure)
        if execution_failed:
            return result
        return result

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

        if agent not in {"claude", "codex"}:
            return AgentResult(AgentResultKind.FAILURE, output=f"unsupported agent: {agent}")
        if self.isolation_config is None:
            isolation_failure = native_isolation_failure()
        elif self.isolation_runtime_executable is None:
            isolation_failure = "native isolation runtime was not supplied by preflight"
        else:
            isolation_failure = verify_native_isolation(
                self.isolation_config,
                enabled_agents=(agent,),
                resolver=lambda _: self.isolation_runtime_executable,
            )
        if isolation_failure is not None:
            return AgentResult(AgentResultKind.FAILURE, output=isolation_failure)

        # Multi-stage workflow prompt and sandbox routing:
        # #280: PLANNING and PLAN_REVIEW are the pre-implementation gate.
        # #281: PR_REVIEW is the post-PR evaluation gate, and REVISING re-dispatches the worker.
        stage = resolve_stage(task)
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
        git_context: IsolationGitContext | None = None
        if self.isolation_config is not None:
            if self.isolation_state_root is None:
                return AgentResult(
                    AgentResultKind.FAILURE,
                    output="native isolation state root was not supplied",
                )
            try:
                git_context = prepare_isolated_git(
                    worktree_path, self.isolation_state_root, task.task_id
                )
            except (OSError, ValueError) as error:
                return AgentResult(AgentResultKind.FAILURE, output=str(error))
        # #296: reuse persisted dispatch_model from task state (e.g. after restart),
        # or resolve from agent_settings if this agent has an explicit stage or default
        # model configured.
        agent_settings = self.agents.get(agent)
        model = (
            task.dispatch_model
            if task.dispatch_model is not None
            else (agent_settings.models.resolve(stage) if agent_settings is not None else None)
        )
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
                    # never actually do anything. This permission mode does not provide
                    # OS isolation; admission above must reject unverified backends.
                    "--permission-mode",
                    "bypassPermissions",
                    "--no-session-persistence",
                    "--strict-mcp-config",
                    "--tools",
                    claude_tools,
                    *(("--model", model) if model else ()),
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
            if self.isolation_config is None:
                return self.claude_agent.execute(req)
            assert git_context is not None
            return self._execute_isolated(
                req, agent=agent, read_only=read_only, git_context=git_context
            )
        elif agent == "codex":
            if self.codex_approval_mode is None:
                return AgentResult(
                    AgentResultKind.FAILURE,
                    output="missing preflight-detected Codex approval mode",
                )
            schema_path = self.codex_output_schema or (
                worktree_path / ".ai" / "codex-output.schema.json"
            )
            ensure_codex_output_schema(schema_path)
            # #314: Codex's own OS-level sandbox conflicts with the outer container
            # boundary, so under container isolation it is told to trust that outer
            # sandbox instead -- see resolve_codex_sandbox_mode().
            codex_sandbox = resolve_codex_sandbox_mode(
                read_only=read_only,
                container_isolated=(
                    self.isolation_config is not None
                    and self.isolation_config.backend == "container"
                ),
            )
            req = ProcessExecutionRequest(
                argv=build_codex_headless_argv(
                    executable="codex",
                    approval_mode=self.codex_approval_mode,
                    sandbox=codex_sandbox,
                    output_schema=schema_path,
                    cwd=worktree_path,
                    model=model,
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
            if self.isolation_config is None:
                return self.codex_agent.execute(req)
            assert git_context is not None
            return self._execute_isolated(
                req, agent=agent, read_only=read_only, git_context=git_context
            )
        return AgentResult(AgentResultKind.FAILURE, output=f"unsupported agent: {agent}")
