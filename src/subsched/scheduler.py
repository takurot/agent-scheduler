from __future__ import annotations

import secrets
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from subsched.contract import bootstrap_task_files
from subsched.events import Clock, EventSource, EventType, SystemClock
from subsched.github.checks import CICheckState, PRChecksStatus
from subsched.github.pull_requests import MergedPrCheckKind, MergedPrCheckResult
from subsched.models import (
    AgentResult,
    AgentResultKind,
    Capacity,
    CapacityState,
    Issue,
    Task,
    TaskState,
    detect_dependency_cycles,
)
from subsched.queue import TaskQueue
from subsched.router import FRESHNESS, Router
from subsched.storage import JsonStateStore
from subsched.structured_logger import StructuredLogger
from subsched.tasks.worktree import WorktreeAdapter


class Worker(Protocol):
    def run(self, task: Task, agent: str) -> AgentResult: ...


class ScriptedWorker:
    def __init__(self, scripts: dict[tuple[int, str], tuple[AgentResult, ...]]) -> None:
        self._scripts = {key: deque(results) for key, results in scripts.items()}
        self.dispatches: list[tuple[int, str]] = []
        self.worktrees: list[tuple[int, str, str | None]] = []

    def run(self, task: Task, agent: str) -> AgentResult:
        self.dispatches.append((task.issue_number, agent))
        self.worktrees.append((task.issue_number, agent, task.worktree))
        try:
            return self._scripts[(task.issue_number, agent)].popleft()
        except (KeyError, IndexError) as error:
            raise RuntimeError(
                f"no scripted result for issue {task.issue_number} and {agent}"
            ) from error


class Scheduler:
    def __init__(
        self,
        *,
        store: JsonStateStore,
        router: Router,
        worker: Worker,
        worktree_root: Path,
        worktree_adapter: WorktreeAdapter | None = None,
        clock: Clock | None = None,
        event_sources: tuple[EventSource, ...] = (),
        verification_commands: tuple[str, ...] = ("true",),
        verification_timeout_seconds: float = 120.0,
        label_scores: dict[str, int] | None = None,
        concurrency: int = 1,
        max_agent_failures: int = 2,
        max_agent_switches: int = 6,
        max_tasks: int = 50,
        push_enabled: bool = False,
        create_pr_enabled: bool = True,
        repo: str | None = None,
        base_branch: str = "main",
        structured_logger: StructuredLogger | None = None,
        # #145: opt-in (default False, unlike config's own default of True) so every
        # existing direct Scheduler(...) construction in the test suite is unaffected --
        # cli.py wires this from cfg.handoff.continuous, giving the config value its
        # actual runtime meaning: continuous handoff is validated (schema, Issue
        # identity, timestamp advancement) by readback at every worker-end boundary,
        # not just trusted as best-effort Agent narration.
        handoff_continuous: bool = False,
        # #141: identifies every event a single Scheduler instance logs across its
        # lifetime (one CLI `run` invocation, in practice) so a JSONL consumer can
        # reconstruct one run's timeline even when multiple runs' events are interleaved
        # in the same log file. Auto-generated when not supplied.
        run_id: str | None = None,
        max_task_runtime_seconds: float | None = None,
        ci_checker: Callable[[int], PRChecksStatus] | None = None,
        merged_pr_checker: Callable[[int], MergedPrCheckResult] | None = None,
    ) -> None:
        self.store = store
        self.router = router
        self.worker = worker
        self.worktree_root = worktree_root.resolve()
        self.worktree_adapter = worktree_adapter
        self.clock = clock or SystemClock()
        self.event_sources = event_sources
        self.structured_logger = structured_logger
        self.handoff_continuous = handoff_continuous
        self.run_id = run_id or secrets.token_hex(6)
        if max_task_runtime_seconds is not None and max_task_runtime_seconds <= 0:
            raise ValueError("max_task_runtime_seconds must be positive")
        self.max_task_runtime_seconds = max_task_runtime_seconds
        self.ci_checker = ci_checker
        self.merged_pr_checker = merged_pr_checker
        self.discovery_notes: tuple[tuple[int, str], ...] = ()
        self.verification_commands = verification_commands
        if verification_timeout_seconds <= 0:
            raise ValueError("verification_timeout_seconds must be positive")
        self.verification_timeout_seconds = verification_timeout_seconds
        self.push_enabled = push_enabled
        self.create_pr_enabled = create_pr_enabled
        self.repo = repo
        self.base_branch = base_branch
        self.concurrency = concurrency
        from subsched.lease import LeaseManager

        self.lease_manager = LeaseManager(max_concurrency=concurrency)
        self.queue = TaskQueue(store.load_tasks(), label_scores=label_scores)
        self.lease_manager.reconcile(self.queue.tasks)
        persisted_capacities = store.load_capacities()
        allowed_cooldowns = {
            CapacityState.COOLDOWN_SESSION,
            CapacityState.COOLDOWN_WEEKLY,
            CapacityState.COOLDOWN_MODEL,
            CapacityState.RATE_LIMITED_TEMPORARY,
        }
        if any(capacity.state not in allowed_cooldowns for capacity in persisted_capacities):
            raise ValueError("persisted capacity must be a scheduler cooldown blocker")
        self._cooldowns = {capacity.agent: capacity for capacity in persisted_capacities}
        if max_agent_failures <= 0:
            raise ValueError("max_agent_failures must be positive")
        if max_agent_switches <= 0 or max_tasks <= 0:
            raise ValueError("scheduler safety limits must be positive")
        self.max_agent_failures = max_agent_failures
        self.max_agent_switches = max_agent_switches
        self.max_tasks = max_tasks
        self.is_waiting_for_capacity = any(
            task.status is TaskState.WAITING_CAPACITY for task in self.tasks
        )
        self._backoff_step = 0

    @property
    def tasks(self) -> tuple[Task, ...]:
        return self.queue.tasks

    def _log(
        self,
        event: str,
        *,
        level: str = "INFO",
        issue_number: int | None = None,
        agent: str | None = None,
        task_id: str | None = None,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        """No-op unless structured_logger is configured. Every event carries run_id (see
        __init__) so a JSONL consumer can reconstruct a single run's timeline. #141:
        message/data are never given raw agent output or issue body text -- callers pass
        only short, structured summaries (counts, states, exit codes, durations); the
        underlying StructuredLogger additionally redacts secret-shaped substrings.
        """
        if self.structured_logger is None:
            return
        merged_data: dict[str, Any] = {"run_id": self.run_id}
        if data:
            merged_data.update(data)
        self.structured_logger.log(
            event,
            level=level,
            issue_number=issue_number,
            agent=agent,
            task_id=task_id,
            message=message,
            data=merged_data,
        )

    def discover(
        self, issues: Iterable[Issue], *, exclude_labels: frozenset[str] = frozenset()
    ) -> None:
        effective_exclude = frozenset({"security-sensitive"}).union(exclude_labels)
        existing = {task.issue_number for task in self.tasks}
        candidates = [
            issue
            for issue in issues
            if issue.number not in existing and not effective_exclude.intersection(issue.labels)
        ]

        additions: list[Task] = []
        notes: list[tuple[int, str]] = []
        for issue in candidates:
            # #146: an open Issue whose implementation PR is already merged must not be
            # rediscovered as READY (duplicate work). A CONFIRMED match (the Scheduler's
            # own PR body/branch convention) is excluded entirely; anything weaker
            # (AMBIGUOUS) fails closed to NEEDS_HUMAN instead of silently proceeding.
            if self.merged_pr_checker is not None:
                check = self.merged_pr_checker(issue.number)
                if check.kind is MergedPrCheckKind.CONFIRMED:
                    notes.append(
                        (
                            issue.number,
                            f"excluded: merged PR #{check.pr_number} already implements "
                            "this issue",
                        )
                    )
                    continue
                if check.kind is MergedPrCheckKind.AMBIGUOUS:
                    flagged = replace(
                        Task.from_issue(issue),
                        status=TaskState.NEEDS_HUMAN,
                        needs_human_reason=check.reason,
                    )
                    additions.append(flagged)
                    notes.append((issue.number, check.reason))
                    continue
            additions.append(Task.from_issue(issue))

        self.discovery_notes = tuple(notes)
        if len(self.tasks) + len(additions) > self.max_tasks:
            raise ValueError(f"task limit exceeded ({self.max_tasks})")
        new_queue = self.queue.append(additions)
        cycles = detect_dependency_cycles(new_queue.tasks)
        if cycles:
            updated_tasks = tuple(
                task.transition(TaskState.BLOCKED)
                if task.issue_number in cycles and task.status is TaskState.WAITING_DEPENDENCY
                else task
                for task in new_queue.tasks
            )
            new_queue = replace(new_queue, tasks=updated_tasks)
        self.queue = new_queue
        self._persist()
        self._log(
            "discovery",
            data={
                "discovered": len(additions),
                "issue_numbers": [a.issue_number for a in additions],
            },
        )

    def tick(
        self,
        capacities: Iterable[Capacity] = (),
        *,
        now: datetime | None = None,
    ) -> bool:
        current = now or self.clock.now()

        for source in self.event_sources:
            for event in source.poll(current):
                if event.event_type == EventType.PAUSE:
                    self.store.set_paused(True)
                elif event.event_type == EventType.RESUME:
                    self.store.set_paused(False)

        if self.store.is_paused():
            return False

        self._expire_overrun_tasks(current)
        self._poll_ci_checks(current)

        supplied = {capacity.agent: capacity for capacity in capacities}
        effective = self._effective_capacities(supplied, current)
        if self.router.select(effective.values(), now=current) is not None:
            self._release_waiting_tasks(current)
        self._release_dependencies(current)
        self.is_waiting_for_capacity = any(
            task.status is TaskState.WAITING_CAPACITY for task in self.tasks
        )

        if self.lease_manager.active_count >= self.concurrency:
            return False

        ready_tasks = [
            t for t in self.queue.ready() if not self.lease_manager.is_task_leased(t.issue_number)
        ]
        if not ready_tasks:
            if self.is_waiting_for_capacity:
                self._backoff_step = min(self._backoff_step + 1, 10)
            return False

        available_capacities = [
            c for c in effective.values() if not self.lease_manager.is_agent_busy(c.agent)
        ]
        agent = self.router.select(available_capacities, now=current)
        if agent is None:
            task = ready_tasks[0]
            self.queue = self.queue.replace(task.transition(TaskState.WAITING_CAPACITY))
            self.is_waiting_for_capacity = True
            self._backoff_step = min(self._backoff_step + 1, 10)
            self._persist()
            return False

        task = ready_tasks[0]
        lease = self.lease_manager.acquire(task.issue_number, agent, now=current)
        try:
            if self.worktree_adapter is not None:
                ctx = self.worktree_adapter.prepare_worktree(task.issue_number)
                task = task.with_worktree(str(ctx.path))
                self.queue = self.queue.replace(task)
            else:
                if task.worktree is None:
                    worktree_path = self.worktree_root / f"issue-{task.issue_number}"
                    task = task.with_worktree(str(worktree_path))
                    self.queue = self.queue.replace(task)
                self._validate_worktree_path(task)
                self._ensure_worktree_directory(task)
                self._validate_worktree(task)

            if task.worktree is not None:
                bootstrap_task_files(Path(task.worktree), task, now=current)

            actual_switches = task.actual_agent_switches
            if task.last_dispatched_agent is not None and task.last_dispatched_agent != agent:
                actual_switches += 1
            dispatched = task.transition(TaskState.DISPATCHED, current_agent=agent, now=current)
            dispatched = replace(
                dispatched,
                actual_agent_switches=actual_switches,
                last_dispatched_agent=agent,
                # #137: set once on first dispatch, preserved on every later attempt
                # (retry, failover, restart) so execution.max_task_runtime is a durable
                # budget for the whole Task, not reset per attempt.
                run_started_at=task.run_started_at or current,
            )
            running = dispatched.transition(TaskState.IN_PROGRESS, current_agent=agent, now=current)
            self.queue = self.queue.replace(running)
            self._persist()
            self._log(
                "dispatch",
                issue_number=running.issue_number,
                agent=agent,
                task_id=running.task_id,
                data={"attempt": running.attempt},
            )

            dispatch_started = time.monotonic()
            try:
                result = self.worker.run(running, agent)
            except Exception as error:
                self._log(
                    "worker_exception",
                    level="ERROR",
                    issue_number=running.issue_number,
                    agent=agent,
                    task_id=running.task_id,
                    message=str(error),
                    data={"exception_type": type(error).__name__, "attempt": running.attempt},
                )
                # AgentResult.output stays minimal (type name only): it becomes part of
                # persisted task state, so it must not carry the exception message even
                # though the structured logger above does (with its own redaction).
                result = AgentResult(AgentResultKind.FAILURE, output=type(error).__name__)

            self._log(
                "agent_finish",
                issue_number=running.issue_number,
                agent=agent,
                task_id=running.task_id,
                data={
                    "result_kind": result.kind.value,
                    "duration_seconds": round(time.monotonic() - dispatch_started, 1),
                    "attempt": running.attempt,
                },
            )

            if self.handoff_continuous and running.worktree is not None:
                escalated = self._enforce_handoff_freshness(running, agent, current, result.kind)
                if escalated is not None:
                    self.queue = self.queue.replace(escalated)
                    self._persist()
                    self._effective_capacities(supplied, current)
                    self._release_dependencies(current)
                    self._backoff_step = 0
                    return True

            self._handle_result(running, agent, result, current)
        finally:
            self.lease_manager.release(task.issue_number, nonce=lease.nonce)
        self._effective_capacities(supplied, current)
        self._release_dependencies(current)
        self._backoff_step = 0
        return True

    def run_until_waiting(
        self,
        capacities: Iterable[Capacity] = (),
        *,
        now: datetime | None = None,
    ) -> None:
        while self.tick(capacities, now=now):
            pass

    def next_reset_at(self, *, now: datetime | None = None) -> datetime | None:
        current = now or self.clock.now()
        future_resets = [
            c.reset_at
            for c in self._cooldowns.values()
            if c.reset_at is not None and c.reset_at > current
        ]
        if future_resets:
            return min(future_resets)
        if self.is_waiting_for_capacity or self._cooldowns:
            backoff_secs = min(900.0, max(60.0, 60.0 * (2 ** min(self._backoff_step, 4))))
            return current + timedelta(seconds=backoff_secs)
        return None

    def wait_duration(self, *, now: datetime | None = None) -> timedelta:
        current = now or self.clock.now()
        target = self.next_reset_at(now=current)
        if target is None:
            return timedelta(0)
        return max(timedelta(0), target - current)

    def refresh_capacities(
        self, capacities: Iterable[Capacity], *, now: datetime | None = None
    ) -> None:
        current = now or self.clock.now()
        supplied = {capacity.agent: capacity for capacity in capacities}
        for agent, cap in supplied.items():
            if (
                cap.state is CapacityState.AVAILABLE
                and cap.source == "provider"
                and cap.confidence == "high"
            ):
                self._cooldowns.pop(agent, None)
        self._effective_capacities(supplied, current)
        if self.router.select(supplied.values(), now=current) is not None:
            self._release_waiting_tasks(current)
        self.is_waiting_for_capacity = any(
            task.status is TaskState.WAITING_CAPACITY for task in self.tasks
        )
        self._persist()

    def manual_wake(self, *, now: datetime | None = None) -> None:
        current = now or self.clock.now()
        self._cooldowns.clear()
        self._release_waiting_tasks(current)
        self.is_waiting_for_capacity = False
        self._backoff_step = 0
        self._persist()

    def _poll_ci_checks(self, now: datetime) -> None:
        """#142: promote READY_FOR_REVIEW to COMPLETE only once CI actually PASSes (when
        CI monitoring is configured); FAIL escalates to NEEDS_HUMAN (no auto-requeue, per
        docs/SPEC.md's "unknown/failed GitHub state is not silently retried" policy, same
        as push/PR-creation/commit-message failures elsewhere in this class);
        PENDING/UNKNOWN leave the task exactly as-is -- an unknown CI state must never be
        promoted to COMPLETE.
        """
        if self.ci_checker is None:
            return
        for task in self.tasks:
            if task.status is not TaskState.READY_FOR_REVIEW or task.pr is None:
                continue
            status = self.ci_checker(task.pr)
            if status.overall_state is CICheckState.PASS:
                updated = task.transition(
                    TaskState.COMPLETE, current_agent=task.current_agent, now=now
                )
                self.queue = self.queue.replace(updated)
                self._persist()
            elif status.overall_state is CICheckState.FAIL:
                from subsched.agents.process import redact_sensitive_command_audit

                failed = ", ".join(
                    c.name for c in status.checks if c.state is CICheckState.FAIL
                ) or "unknown check"
                reason = "\n".join(
                    redact_sensitive_command_audit(
                        (f"CI failed for PR #{task.pr}: {failed}",)
                    )
                )
                updated = task.transition(
                    TaskState.NEEDS_HUMAN,
                    current_agent=task.current_agent,
                    now=now,
                    reason=reason,
                )
                self.queue = self.queue.replace(updated)
                self._persist()
            # PENDING/UNKNOWN: leave the task in READY_FOR_REVIEW unchanged.

    def _finalize_verified_task(
        self, verifying: Task, agent: str, now: datetime, verification_summary: str
    ) -> Task:
        """Complete a task that has passed verification: rebase, push, and open/reuse its PR.

        When push_enabled is False (the default, and what every existing test uses), when
        create_pr_enabled is False (#136: github.completion.create_pr=false must actually
        disable GitHub writes, not just be parsed and ignored), or the task has no worktree,
        this only advances local task state to COMPLETE -- no git or GitHub calls are made.
        This keeps the change additive: nothing that already worked without push/PR wiring
        changes behavior.
        """
        if not self.push_enabled or not self.create_pr_enabled or verifying.worktree is None:
            pr_ready = verifying.transition(TaskState.PR_READY, current_agent=agent, now=now)
            ready_for_review = pr_ready.transition(
                TaskState.READY_FOR_REVIEW, current_agent=agent, now=now
            )
            return ready_for_review.transition(TaskState.COMPLETE, current_agent=agent, now=now)

        from subsched.github.conflict import handle_rebase_outcome, rebase_onto_base
        from subsched.github.pull_requests import (
            PullRequestResultKind,
            create_or_get_pull_request,
            find_close_keyword_commits,
        )
        from subsched.github.push import PushResultKind, push_task_branch

        worktree_dir = Path(verifying.worktree)
        branch_name = f"subsched/issue-{verifying.issue_number}"

        rebase_result = rebase_onto_base(worktree_dir, base_branch=self.base_branch)
        after_rebase, _rebase_msg = handle_rebase_outcome(verifying, rebase_result)
        self._log(
            "rebase",
            issue_number=verifying.issue_number,
            agent=agent,
            task_id=verifying.task_id,
            data={
                "escalated": after_rebase.status is not verifying.status,
                "attempt": verifying.attempt,
            },
        )
        if after_rebase.status is not verifying.status:
            # handle_rebase_outcome escalated (e.g. NEEDS_HUMAN) on conflict/failure; the
            # reason is already attached to after_rebase.needs_human_reason.
            return after_rebase

        # #140: never push a commit whose message contains a GitHub auto-close keyword
        # (Fixes/Closes/Resolves #N) -- that would let a merge auto-close the issue,
        # bypassing the "issues stay open until manual review" invariant. This never
        # rewrites history; it only inspects and, on any violation (including an
        # inconclusive git failure), fails closed to NEEDS_HUMAN instead of pushing.
        violations = find_close_keyword_commits(worktree_dir, self.base_branch)
        if violations is None or violations:
            if violations is None:
                reason = (
                    "could not verify commit messages are free of GitHub auto-close "
                    "keywords (git log failed); failing closed before push"
                )
            else:
                joined = "; ".join(f"{v.commit}: {v.keyword_context}" for v in violations)
                reason = (
                    "commit message(s) contain GitHub auto-close keywords "
                    f"(Fixes/Closes/Resolves #N): {joined}"
                )
            return verifying.transition(
                TaskState.NEEDS_HUMAN, current_agent=agent, now=now, reason=reason
            )

        push_result = push_task_branch(worktree_dir, branch_name)
        self._log(
            "push",
            issue_number=verifying.issue_number,
            agent=agent,
            task_id=verifying.task_id,
            data={"result_kind": push_result.kind.value, "attempt": verifying.attempt},
        )
        if push_result.kind is not PushResultKind.SUCCESS:
            reason = f"push failed ({push_result.kind.value}): {push_result.output}"
            return verifying.transition(
                TaskState.NEEDS_HUMAN, current_agent=agent, now=now, reason=reason
            )

        pr_result = create_or_get_pull_request(
            verifying,
            branch_name,
            base=self.base_branch,
            repo=self.repo,
            verification_summary=verification_summary,
        )
        if pr_result.kind is not PullRequestResultKind.SUCCESS or pr_result.info is None:
            reason = f"PR creation failed: {pr_result.output}"
            return verifying.transition(
                TaskState.NEEDS_HUMAN, current_agent=agent, now=now, reason=reason
            )
        pr_info = pr_result.info
        self._log(
            "pr_created",
            issue_number=verifying.issue_number,
            agent=agent,
            task_id=verifying.task_id,
            data={"pr_number": pr_info.number, "attempt": verifying.attempt},
        )

        with_pr = replace(verifying, pr=pr_info.number)
        pr_ready = with_pr.transition(TaskState.PR_READY, current_agent=agent, now=now)
        # #142: stop here, not COMPLETE. Opening a PR means the Scheduler's own local
        # verification passed, nothing more -- CI hasn't been checked (or may not even
        # exist yet) and no human has reviewed anything. READY_FOR_REVIEW is now the
        # default terminal state for this path; COMPLETE is only reached via CI
        # monitoring (see _poll_ci_checks) confirming CI PASS, when enabled.
        return pr_ready.transition(TaskState.READY_FOR_REVIEW, current_agent=agent, now=now)

    # #145 code review: an externally-imposed interruption (capacity cutoff, timeout) is
    # not evidence the Agent failed to follow the handoff contract -- the Agent may have
    # had no opportunity to write anything at all, e.g. capacity exhausted the instant
    # the process started. Escalating those straight to NEEDS_HUMAN on their very first
    # occurrence would collapse the SPEC's normal capacity-driven agent-switch/retry
    # design (cooldown tracking, max_agent_switches, requeue_after_capacity_event) into a
    # human-escalation event before that design ever gets a chance to run. Readback is
    # still performed and logged for these kinds (satisfying "every worker-end boundary
    # is readback-validated"), but only PASS and genuine Agent-failure kinds actually
    # escalate on a stale/invalid handoff.
    _HANDOFF_NON_ESCALATING_KINDS = frozenset(
        {
            AgentResultKind.CAPACITY_SESSION,
            AgentResultKind.CAPACITY_WEEKLY,
            AgentResultKind.CAPACITY_TEMPORARY,
            AgentResultKind.TIMEOUT,
        }
    )

    def _enforce_handoff_freshness(
        self, task: Task, agent: str, dispatched_at: datetime, result_kind: AgentResultKind
    ) -> Task | None:
        """#145: readback-validate the handoff at every worker-end boundary (normal,
        capacity, timeout, failure -- called unconditionally right after worker.run()
        returns, before any outcome-specific handling) so `handoff.continuous` has an
        actual runtime-observable meaning instead of being a best-effort natural-language
        instruction the Agent may or may not follow. A stale/invalid handoff is still
        allowed to continue if a mechanical checkpoint (captured by the Scheduler itself,
        not self-reported by the Agent) proves the same or newer progress happened;
        otherwise the task is escalated directly to NEEDS_HUMAN -- except for capacity/
        timeout outcomes (see _HANDOFF_NON_ESCALATING_KINDS), which are logged but never
        forced to escalate, since those are externally-imposed interruptions rather than
        Agent non-compliance. Returns the escalated Task, or None if the caller should
        proceed with its normal result handling.
        """
        assert task.worktree is not None
        from subsched.handoff import can_recover_from_checkpoint, readback_handoff

        worktree_dir = Path(task.worktree)
        readback = readback_handoff(worktree_dir, task, dispatched_at=dispatched_at)
        if readback.ok:
            return None
        if can_recover_from_checkpoint(worktree_dir, task, dispatched_at=dispatched_at):
            if self.structured_logger is not None:
                self.structured_logger.log(
                    "handoff_readback",
                    level="WARN",
                    issue_number=task.issue_number,
                    agent=agent,
                    task_id=task.task_id,
                    message=readback.reason,
                    data={"recovered_from_checkpoint": True},
                )
            return None
        non_escalating = result_kind in self._HANDOFF_NON_ESCALATING_KINDS
        if self.structured_logger is not None:
            self.structured_logger.log(
                "handoff_readback",
                level="WARN" if non_escalating else "ERROR",
                issue_number=task.issue_number,
                agent=agent,
                task_id=task.task_id,
                message=readback.reason,
                data={"recovered_from_checkpoint": False, "escalated": not non_escalating},
            )
        if non_escalating:
            return None
        return task.transition(
            TaskState.NEEDS_HUMAN, current_agent=agent, now=dispatched_at, reason=readback.reason
        )

    def _handle_result(self, task: Task, agent: str, result: AgentResult, now: datetime) -> None:
        if result.kind is AgentResultKind.PASS:
            verifying = task.transition(TaskState.VERIFYING, current_agent=agent, now=now)
            self.queue = self.queue.replace(verifying)
            self._persist()

            verification_ok = True
            verification_summary = ""
            if task.worktree is not None:
                from subsched.checkpoint import capture_mechanical_checkpoint, save_checkpoint
                from subsched.verification import run_verification

                self._log(
                    "verification_start",
                    issue_number=task.issue_number,
                    agent=agent,
                    task_id=task.task_id,
                    data={"commands": len(self.verification_commands), "attempt": task.attempt},
                )
                v_report = run_verification(
                    Path(task.worktree),
                    self.verification_commands,
                    timeout_seconds=self.verification_timeout_seconds,
                )
                verification_ok = v_report.passed
                verification_summary = v_report.summary
                self._log(
                    "gate_result",
                    issue_number=task.issue_number,
                    agent=agent,
                    task_id=task.task_id,
                    data={"passed": verification_ok, "attempt": task.attempt},
                )
                cp = capture_mechanical_checkpoint(
                    Path(task.worktree),
                    task.issue_number,
                    result,
                    exit_code=0 if verification_ok else 1,
                    test_results=v_report.summary,
                )
                save_checkpoint(Path(task.worktree), cp)
                self._log(
                    "checkpoint",
                    issue_number=task.issue_number,
                    agent=agent,
                    task_id=task.task_id,
                    data={"attempt": task.attempt},
                )

            if verification_ok:
                final_task = self._finalize_verified_task(
                    verifying, agent, now, verification_summary
                )
                self.queue = self.queue.replace(final_task)
                self._log(
                    "task_transition",
                    issue_number=final_task.issue_number,
                    agent=agent,
                    task_id=final_task.task_id,
                    data={
                        "from_state": verifying.status.value,
                        "to_state": final_task.status.value,
                        "attempt": final_task.attempt,
                    },
                )
            else:
                retry = verifying.transition(
                    TaskState.RETRY, current_agent=None, increment_attempt=True, now=now
                )
                next_state = (
                    TaskState.NEEDS_HUMAN
                    if retry.attempt >= self.max_agent_failures
                    else TaskState.READY
                )
                final = retry.transition(next_state, now=now)
                self.queue = self.queue.replace(final)
                self._log(
                    "task_transition",
                    issue_number=final.issue_number,
                    agent=agent,
                    task_id=final.task_id,
                    data={
                        "from_state": verifying.status.value,
                        "to_state": final.status.value,
                        "attempt": final.attempt,
                    },
                )
        elif result.kind in {AgentResultKind.CAPACITY_SESSION, AgentResultKind.CAPACITY_WEEKLY}:
            state = (
                CapacityState.COOLDOWN_SESSION
                if result.kind is AgentResultKind.CAPACITY_SESSION
                else CapacityState.COOLDOWN_WEEKLY
            )
            self._cooldowns = {
                **self._cooldowns,
                agent: Capacity(
                    agent=agent,
                    state=state,
                    reset_at=result.reset_at,
                    observed_at=now,
                    source="structured_result",
                    confidence="high",
                ),
            }
            new_capacity_events = task.capacity_events + 1
            switched = replace(
                task,
                agent_switches=task.agent_switches + 1,
                capacity_events=new_capacity_events,
            )
            self.queue = self.queue.replace(switched)
            if switched.agent_switches >= self.max_agent_switches:
                waiting = switched.transition(TaskState.WAITING_CAPACITY, current_agent=None)
                final_capacity_task = waiting.transition(TaskState.NEEDS_HUMAN, current_agent=None)
                self.queue = self.queue.replace(final_capacity_task)
                self._log(
                    "task_transition",
                    issue_number=final_capacity_task.issue_number,
                    agent=agent,
                    task_id=final_capacity_task.task_id,
                    data={
                        "from_state": task.status.value,
                        "to_state": final_capacity_task.status.value,
                        "capacity_state": state.value,
                        "attempt": final_capacity_task.attempt,
                    },
                )
            else:
                self.queue = self.queue.requeue_after_capacity_event(task.issue_number)
                self._log(
                    "task_transition",
                    issue_number=task.issue_number,
                    agent=agent,
                    task_id=task.task_id,
                    data={
                        "from_state": task.status.value,
                        "to_state": TaskState.READY.value,
                        "capacity_state": state.value,
                        "attempt": task.attempt,
                    },
                )
        else:
            failures_dict = dict(task.per_agent_failures)
            failures_dict[agent] = failures_dict.get(agent, 0) + 1
            per_agent = tuple(sorted(failures_dict.items()))
            retry = task.transition(
                TaskState.RETRY,
                current_agent=None,
                increment_attempt=True,
                now=now,
            )
            retry = replace(retry, per_agent_failures=per_agent)
            next_state = (
                TaskState.NEEDS_HUMAN
                if retry.attempt >= self.max_agent_failures
                else TaskState.READY
            )
            final = retry.transition(next_state, now=now)
            self.queue = self.queue.replace(final)
            self._log(
                "task_transition",
                issue_number=final.issue_number,
                agent=agent,
                task_id=final.task_id,
                data={
                    "from_state": task.status.value,
                    "to_state": final.status.value,
                    "attempt": final.attempt,
                },
            )
        self._persist()

    def _effective_capacities(
        self, supplied: dict[str, Capacity], now: datetime
    ) -> dict[str, Capacity]:
        active_cooldowns: dict[str, Capacity] = {}
        for name, capacity in self._cooldowns.items():
            probe = supplied.get(name)
            reset_at = capacity.reset_at
            has_fresh_available_probe = (
                reset_at is not None
                and probe is not None
                and now >= reset_at
                and probe.state is CapacityState.AVAILABLE
                and probe.source == "provider"
                and probe.confidence == "high"
                and probe.observed_at >= reset_at
                and timedelta(0) <= now - probe.observed_at <= FRESHNESS
            )
            if not has_fresh_available_probe:
                active_cooldowns = {**active_cooldowns, name: capacity}
        self._cooldowns = active_cooldowns
        return {**supplied, **active_cooldowns}

    _RUNTIME_EXPIRABLE_STATES = frozenset(
        {
            TaskState.READY,
            TaskState.WAITING_CAPACITY,
            TaskState.RETRY,
            TaskState.WAITING_DEPENDENCY,
            TaskState.BLOCKED,
        }
    )

    def _expire_overrun_tasks(self, now: datetime) -> None:
        """#137: enforce execution.max_task_runtime as a durable budget covering the
        whole Task (across failover/retry/restart), not a single attempt. Runs before
        dispatch on every tick so a task whose budget is already exhausted is never
        redispatched. Escalates to NEEDS_HUMAN (fail-closed, no silent drop) rather than
        a terminal FAILED/CANCELLED, since the work may still be valuable and just needs
        a human decision (matches the fail-closed pattern used elsewhere in this class).
        """
        if self.max_task_runtime_seconds is None:
            return
        for task in self.tasks:
            if task.status not in self._RUNTIME_EXPIRABLE_STATES or task.run_started_at is None:
                continue
            elapsed = (now - task.run_started_at).total_seconds()
            if elapsed < self.max_task_runtime_seconds:
                continue
            reason = (
                f"execution.max_task_runtime exceeded ({int(elapsed)}s >= "
                f"{int(self.max_task_runtime_seconds)}s since first dispatch)"
            )
            updated = task.transition(
                TaskState.NEEDS_HUMAN, current_agent=task.current_agent, now=now, reason=reason
            )
            self.queue = self.queue.replace(updated)
            self._persist()

    def _release_waiting_tasks(self, now: datetime) -> None:
        for task in self.tasks:
            if task.status is TaskState.WAITING_CAPACITY:
                self.queue = self.queue.replace(task.transition(TaskState.READY, now=now))

    def _release_dependencies(self, now: datetime) -> None:
        completed = {task.issue_number for task in self.tasks if task.status is TaskState.COMPLETE}
        terminal_failed = {
            task.issue_number
            for task in self.tasks
            if task.status
            in {
                TaskState.FAILED,
                TaskState.CANCELLED,
                TaskState.BLOCKED,
                TaskState.NEEDS_HUMAN,
            }
        }
        all_known = {task.issue_number for task in self.tasks}
        for task in self.tasks:
            if task.status is TaskState.WAITING_DEPENDENCY:
                if set(task.dependencies) <= completed:
                    self.queue = self.queue.replace(task.transition(TaskState.READY, now=now))
                elif any(
                    dep in terminal_failed or dep not in all_known for dep in task.dependencies
                ):
                    self.queue = self.queue.replace(task.transition(TaskState.BLOCKED, now=now))

    def _validate_worktree(self, task: Task) -> None:
        self._validate_worktree_path(task)
        if task.worktree is None:
            raise ValueError("task worktree is missing")
        candidate = Path(task.worktree)
        if not candidate.is_dir():
            raise ValueError(f"task #{task.issue_number} worktree is not a directory")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.worktree_root):
            raise ValueError(f"task #{task.issue_number} worktree escapes its root")

    def _validate_worktree_path(self, task: Task) -> None:
        if task.worktree is None:
            raise ValueError("task worktree is missing")
        expected = self.worktree_root / f"issue-{task.issue_number}"
        candidate = Path(task.worktree)
        if candidate != expected or candidate.is_symlink():
            raise ValueError(f"task #{task.issue_number} has an invalid worktree path")

    def _ensure_worktree_directory(self, task: Task) -> None:
        if task.worktree is None:
            raise ValueError("task worktree is missing")
        candidate = Path(task.worktree)
        if candidate.is_symlink():
            raise ValueError(f"task #{task.issue_number} has a symlinked worktree")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _persist(self) -> None:
        # Serialize with CLI writers (pause/resume/cancel/run) via the same process-level
        # lock they use, so a concurrent command cannot interleave with a tick's write and
        # silently lose either side's update. expected_revision is re-read inside the lock
        # (so it always matches at write time under correct lock usage) and is kept as a
        # defense-in-depth CAS check in case that invariant is ever violated.
        with self.store.lock():
            expected_revision = self.store.get_revision()
            self.store.save_state(
                self.tasks,
                paused=self.store.is_paused(),
                capacities=self._cooldowns.values(),
                expected_revision=expected_revision,
            )
