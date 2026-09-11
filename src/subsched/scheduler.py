from __future__ import annotations

import logging
import os
import secrets
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from subsched.config import validate_base_branch
from subsched.contract import bootstrap_task_files
from subsched.events import Clock, Event, EventSource, EventType, SystemClock
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
    parse_dependencies,
)
from subsched.queue import TaskQueue
from subsched.recovery import (
    ProcessRecord,
    clear_process_record,
    escalate_to_needs_human,
    reconcile_task_recovery,
    save_process_record,
)
from subsched.router import FRESHNESS, Router
from subsched.storage import JsonStateStore, get_process_start_time
from subsched.structured_logger import StructuredLogger
from subsched.tasks.worktree import WorktreeAdapter, WorktreeError

logger = logging.getLogger(__name__)

_IN_FLIGHT_RECOVERY_STATES = frozenset(
    {TaskState.DISPATCHED, TaskState.IN_PROGRESS, TaskState.VERIFYING}
)
_ALLOWED_COOLDOWNS = frozenset(
    {
        CapacityState.COOLDOWN_SESSION,
        CapacityState.COOLDOWN_WEEKLY,
        CapacityState.COOLDOWN_MODEL,
        CapacityState.RATE_LIMITED_TEMPORARY,
        CapacityState.AUTH_ERROR,
        CapacityState.DISABLED_BILLING,
    }
)


def _parse_capacity_item(item: Any) -> Capacity | None:
    if isinstance(item, Capacity):
        return item
    if isinstance(item, dict):
        try:
            kwargs = dict(item)
            if "state" in kwargs and isinstance(kwargs["state"], str):
                kwargs["state"] = CapacityState(kwargs["state"])
            if "observed_at" in kwargs and isinstance(kwargs["observed_at"], str):
                kwargs["observed_at"] = datetime.fromisoformat(kwargs["observed_at"])
            if "reset_at" in kwargs and isinstance(kwargs["reset_at"], str):
                kwargs["reset_at"] = datetime.fromisoformat(kwargs["reset_at"])
            return Capacity(**kwargs)
        except (ValueError, TypeError, KeyError):
            return None
    return None


def _extract_capacities_from_payload(payload: dict[str, Any]) -> list[Capacity]:
    items: list[Capacity] = []
    if "capacities" in payload:
        raw_list = payload["capacities"]
        if isinstance(raw_list, Iterable) and not isinstance(raw_list, (str, bytes)):
            for item in raw_list:
                parsed = _parse_capacity_item(item)
                if parsed is not None:
                    items.append(parsed)
    if "capacity" in payload:
        parsed = _parse_capacity_item(payload["capacity"])
        if parsed is not None:
            items.append(parsed)
    return items


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
        # #175: verification-gate failures previously shared `task.attempt` with
        # genuine Agent failures, so `max_agent_failures` could escalate to
        # NEEDS_HUMAN after a single real Agent failure preceded by verification
        # retries. Tracked as its own durable budget via Task.verification_failures.
        max_verification_failures: int = 2,
        max_agent_switches: int = 6,
        max_tasks: int = 50,
        push_enabled: bool = False,
        create_pr_enabled: bool = True,
        close_issue_enabled: bool = False,
        repo: str | None = None,
        base_branch: str | None = None,
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
        self.close_issue_enabled = close_issue_enabled
        self.repo = repo
        if push_enabled and create_pr_enabled and base_branch is None:
            raise ValueError("base_branch must be resolved before enabling push/PR")
        self.base_branch = (
            validate_base_branch(base_branch) if base_branch is not None else None
        )
        self.concurrency = concurrency
        if max_agent_failures <= 0:
            raise ValueError("max_agent_failures must be positive")
        if max_verification_failures <= 0:
            raise ValueError("max_verification_failures must be positive")
        if max_agent_switches <= 0 or max_tasks <= 0:
            raise ValueError("scheduler safety limits must be positive")
        self.max_agent_failures = max_agent_failures
        self.max_verification_failures = max_verification_failures
        self.max_agent_switches = max_agent_switches
        self.max_tasks = max_tasks
        from subsched.lease import LeaseManager

        self.lease_manager = LeaseManager(max_concurrency=concurrency)
        self.queue = TaskQueue(store.load_tasks(), label_scores=label_scores)
        persisted_capacities = store.load_capacities()
        if any(capacity.state not in _ALLOWED_COOLDOWNS for capacity in persisted_capacities):
            raise ValueError("persisted capacity must be a scheduler cooldown blocker")
        self._cooldowns = {capacity.agent: capacity for capacity in persisted_capacities}
        # #164: resolve any task still DISPATCHED/IN_PROGRESS from a prior process
        # (crash, kill, host restart) before the lease manager re-leases it -- otherwise
        # an unresolved in-flight task holds a permanent lease and silently blocks every
        # other READY task forever. Must run after self._cooldowns is set (_persist()
        # reads it) and before lease_manager.reconcile() (which must see the resolved
        # states, not the stale ones).
        self._reconcile_recovery()
        self.lease_manager.reconcile(self.queue.tasks)
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

    def _reconcile_recovery(self) -> None:
        """#164, #203: reconcile every DISPATCHED/IN_PROGRESS/VERIFYING task against its
        recorded process or checkpoint before the lease manager re-registers it.
        `reconcile_task_recovery`'s own RETRY result is not queueable by itself --
        nothing else in this class transitions RETRY -> READY, it is always resolved by
        its caller in the same step -- so the crash is recorded against the dispatched
        Agent and resolved here into READY or NEEDS_HUMAN using the same per-agent-failure
        threshold `_handle_result` uses for a live agent failure. An interrupted VERIFYING
        task is reconciled via _reconcile_verifying_task into a safe resumable state.
        """
        in_flight = [
            task for task in self.queue.tasks if task.status in _IN_FLIGHT_RECOVERY_STATES
        ]
        changed = False
        for task in in_flight:
            from_state = task.status
            if task.status is TaskState.VERIFYING:
                resolved, reason = self._reconcile_verifying_task(task)
            else:
                if (
                    task.current_agent is not None
                    and task.last_dispatched_agent is not None
                    and task.current_agent != task.last_dispatched_agent
                ):
                    recovery_agent = None
                    recovery_agent_error = "dispatched Agent identity inconsistent"
                else:
                    recovery_agent = task.current_agent or task.last_dispatched_agent
                    recovery_agent_error = "dispatched Agent identity missing"
                if task.worktree is None:
                    # No worktree recorded at all: there is no process record, no handoff,
                    # and no way to verify what happened. Fail-closed rather than resume
                    # blindly. Routed through recovery.escalate_to_needs_human (not a plain
                    # .transition() call) because DISPATCHED cannot transition directly to
                    # NEEDS_HUMAN -- see ALLOWED_TRANSITIONS in models.py.
                    reason = "no worktree recorded for in-flight task; escalated to NEEDS_HUMAN"
                    resolved = escalate_to_needs_human(task, reason)
                else:
                    reconciled, reason = reconcile_task_recovery(Path(task.worktree), task)
                    if reconciled.status is TaskState.RETRY:
                        if recovery_agent is None:
                            reason = (
                                f"{reason}; {recovery_agent_error}; "
                                "escalated to NEEDS_HUMAN"
                            )
                            resolved = reconciled.transition(TaskState.NEEDS_HUMAN, reason=reason)
                        else:
                            failures_dict = dict(reconciled.per_agent_failures)
                            failures_dict[recovery_agent] = failures_dict.get(recovery_agent, 0) + 1
                            agent_failure_count = failures_dict[recovery_agent]
                            reconciled = replace(
                                reconciled,
                                per_agent_failures=tuple(sorted(failures_dict.items())),
                            )
                            next_state = (
                                TaskState.NEEDS_HUMAN
                                if agent_failure_count >= self.max_agent_failures
                                else TaskState.READY
                            )
                            resolved = reconciled.transition(next_state, reason=reason)
                    else:
                        resolved = reconciled

            if resolved.status is from_state:
                continue
            changed = True
            self.queue = self.queue.replace(resolved)

            self._log(
                "recovery",
                issue_number=resolved.issue_number,
                task_id=resolved.task_id,
                message=reason,
                data={"from_state": from_state.value, "to_state": resolved.status.value},
            )
        if changed:
            self._persist()

    def _reconcile_verifying_task(self, task: Task) -> tuple[Task, str]:
        """#203: reconcile an interrupted VERIFYING task on startup.
        If a PR already exists, advance to READY_FOR_REVIEW without duplicating side effects.
        Otherwise, if worktree/handoff are valid, return the task to READY (or escalate
        to NEEDS_HUMAN if the attempt threshold is reached) so the task is not stranded.
        Fail closed to NEEDS_HUMAN if worktree or handoff is invalid.
        """
        if task.worktree is None:
            reason = "no worktree recorded for verifying task; escalated to NEEDS_HUMAN"
            return task.transition(TaskState.NEEDS_HUMAN, reason=reason), reason

        worktree_dir = Path(task.worktree)
        if not worktree_dir.exists() or not worktree_dir.is_dir() or worktree_dir.is_symlink():
            reason = "worktree invalid or missing for verifying task; escalated to NEEDS_HUMAN"
            return task.transition(TaskState.NEEDS_HUMAN, reason=reason), reason

        from subsched.handoff import reconstruct_or_quarantine_handoff

        handoff_ok = reconstruct_or_quarantine_handoff(worktree_dir, task)
        if not handoff_ok:
            reason = (
                "handoff was corrupted and quarantined for verifying task; escalated to NEEDS_HUMAN"
            )
            return task.transition(TaskState.NEEDS_HUMAN, reason=reason), reason

        # If push and PR creation are enabled and a repo is configured,
        # check if a remote PR was already created before interruption.
        if self.push_enabled and self.create_pr_enabled and self.repo is not None:
            from subsched.github.pull_requests import (
                ExistingPrCheckKind,
                lookup_existing_pr,
            )

            branch_name = f"subsched/issue-{task.issue_number}"
            existing_pr = lookup_existing_pr(
                branch_name,
                issue_number=task.issue_number,
                base=self.base_branch or "main",
                repo=self.repo,
            )
            if (
                existing_pr.kind is ExistingPrCheckKind.CONFIRMED
                and existing_pr.info is not None
            ):
                with_pr = replace(task, pr=existing_pr.info.number)
                reason = (
                    f"interrupted verifying task already has PR #{existing_pr.info.number}; "
                    "recovered to READY_FOR_REVIEW"
                )
                pr_ready = with_pr.transition(TaskState.PR_READY, reason=reason)
                ready_for_review = pr_ready.transition(
                    TaskState.READY_FOR_REVIEW, reason=reason
                )
                return ready_for_review, reason

        # Otherwise, verification was interrupted; recover to READY (or
        # NEEDS_HUMAN if attempt limit exceeded).
        reason = "verification interrupted before completion; recovered to READY"
        retry = task.transition(
            TaskState.RETRY, current_agent=None, increment_attempt=True, reason=reason
        )
        next_state = (
            TaskState.NEEDS_HUMAN
            if retry.attempt >= self.max_agent_failures
            else TaskState.READY
        )
        return retry.transition(next_state, reason=reason), reason


    def discover(
        self,
        issues: Iterable[Issue],
        *,
        exclude_labels: frozenset[str] = frozenset(),
        snapshot_complete: bool = False,
    ) -> None:
        effective_exclude = frozenset({"security-sensitive"}).union(exclude_labels)
        issues_list = list(issues)
        issues_by_number = {issue.number: issue for issue in issues_list}
        existing_numbers = {task.issue_number for task in self.tasks}

        # #185: Reconcile persisted non-terminal tasks against current GitHub issue state
        reconciled_tasks: list[Task] = []
        for task in self.tasks:
            # Terminal states (COMPLETE, FAILED, CANCELLED) are immutable historical records
            if task.status in (TaskState.COMPLETE, TaskState.FAILED, TaskState.CANCELLED):
                reconciled_tasks.append(task)
                continue

            # In-flight tasks are guarded by leases/worktrees and not mutated silently
            if task.status in _IN_FLIGHT_RECOVERY_STATES:
                reconciled_tasks.append(task)
                continue

            # Case 1: Issue is missing from snapshot
            if task.issue_number not in issues_by_number:
                if snapshot_complete and task.status is not TaskState.NEEDS_HUMAN:
                    reason = "issue missing from complete GitHub snapshot"
                    task = task.transition(TaskState.NEEDS_HUMAN, reason=reason)
                reconciled_tasks.append(task)
                continue

            # Case 2: Issue exists in snapshot
            issue = issues_by_number[task.issue_number]
            new_deps = parse_dependencies(issue.body)
            # Deterministically refresh safe metadata (title, body, labels, dependencies)
            task = replace(
                task,
                title=issue.title,
                description=issue.body,
                labels=issue.labels,
                dependencies=new_deps,
            )

            # Check newly excluded labels
            if effective_exclude.intersection(issue.labels):
                if task.status is not TaskState.NEEDS_HUMAN:
                    reason = "issue is no longer eligible: excluded label"
                    task = task.transition(TaskState.NEEDS_HUMAN, reason=reason)
                reconciled_tasks.append(task)
                continue

            # Re-evaluate dependency state transitions
            if task.issue_number in new_deps:
                if task.status is not TaskState.BLOCKED:
                    task = task.transition(TaskState.BLOCKED, reason="self-dependency detected")
            elif task.status is TaskState.READY and new_deps:
                task = task.transition(TaskState.WAITING_DEPENDENCY)
            elif (
                task.status in (TaskState.WAITING_DEPENDENCY, TaskState.BLOCKED)
                and not new_deps
            ):
                task = task.transition(TaskState.READY)

            reconciled_tasks.append(task)

        # Update queue with reconciled existing tasks
        new_queue = replace(self.queue, tasks=tuple(reconciled_tasks))

        candidates = [
            issue
            for issue in issues_list
            if (
                issue.number not in existing_numbers
                and not effective_exclude.intersection(issue.labels)
            )
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
        if len(new_queue.tasks) + len(additions) > self.max_tasks:
            raise ValueError(f"task limit exceeded ({self.max_tasks})")
        new_queue = new_queue.append(additions)
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
        self._release_dependencies(self.clock.now())
        self._persist()
        self._log(
            "discovery",
            data={
                "discovered": len(additions),
                "issue_numbers": [a.issue_number for a in additions],
            },
        )

    def _handle_event(
        self,
        event: Event,
        current: datetime,
        event_capacities: dict[str, Capacity],
    ) -> None:
        if event.event_type == EventType.PAUSE:
            self.store.set_paused(True)
            self._log("event_received", data={"event_type": event.event_type.value})
        elif event.event_type == EventType.RESUME:
            self.store.set_paused(False)
            self._log("event_received", data={"event_type": event.event_type.value})
        elif event.event_type == EventType.CAPACITY_PROBE:
            extracted = _extract_capacities_from_payload(event.payload)
            if not extracted:
                logger.warning("CAPACITY_PROBE event missing capacity payload: %s", event)
                self._log(
                    "event_unhandled_payload",
                    level="WARNING",
                    message="CAPACITY_PROBE event missing capacity payload",
                    data={"payload": event.payload},
                )
            else:
                for cap in extracted:
                    event_capacities[cap.agent] = cap
                    if cap.state in _ALLOWED_COOLDOWNS:
                        self._cooldowns[cap.agent] = cap
                self._persist()
                self._log(
                    "event_received",
                    data={
                        "event_type": event.event_type.value,
                        "agents": list(event_capacities.keys()),
                    },
                )
        elif event.event_type == EventType.CAPACITY_RESET:
            extracted = _extract_capacities_from_payload(event.payload)
            target_agent: str | None = event.payload.get("agent")
            if extracted:
                for cap in extracted:
                    event_capacities[cap.agent] = cap

            agents_to_check: list[str] = (
                [target_agent] if target_agent is not None else list(self._cooldowns.keys())
            )
            for agent in agents_to_check:
                cooldown = self._cooldowns.get(agent)
                probe = event_capacities.get(agent)
                if cooldown is not None:
                    reset_at = cooldown.reset_at
                    has_fresh_available_probe = (
                        reset_at is not None
                        and probe is not None
                        and current >= reset_at
                        and probe.state is CapacityState.AVAILABLE
                        and probe.source == "provider"
                        and probe.confidence == "high"
                        and probe.observed_at >= reset_at
                        and timedelta(0) <= current - probe.observed_at <= FRESHNESS
                    )
                    if has_fresh_available_probe:
                        self._cooldowns.pop(agent, None)
                        self._backoff_step = 0
                        self._log(
                            "capacity_reset_cleared",
                            agent=agent,
                            message="cooldown cleared via fresh provider probe",
                        )
                    else:
                        self._backoff_step = 0
                        logger.warning(
                            "CAPACITY_RESET received for agent %s without fresh "
                            "high-confidence provider probe; cooldown retained",
                            agent,
                        )
                        self._log(
                            "capacity_reset_unverified",
                            level="WARNING",
                            agent=agent,
                            message=(
                                "CAPACITY_RESET received without fresh high-confidence "
                                "provider probe; cooldown retained"
                            ),
                            data={"payload": event.payload},
                        )
            self._persist()
        elif event.event_type == EventType.TASK_COMPLETED:
            issue_number = event.payload.get("issue_number")
            task_id = event.payload.get("task_id")
            if issue_number is None and task_id is None:
                logger.warning("TASK_COMPLETED event missing issue_number and task_id: %s", event)
                self._log(
                    "event_unhandled_payload",
                    level="WARNING",
                    message="TASK_COMPLETED missing issue_number and task_id",
                )
                return

            matching: list[Task] = []
            if issue_number is not None:
                matching = [t for t in self.tasks if t.issue_number == issue_number]
            elif task_id is not None:
                matching = [t for t in self.tasks if t.task_id == task_id]

            if not matching:
                logger.warning(
                    "TASK_COMPLETED event for unknown task: issue=%s, task_id=%s",
                    issue_number,
                    task_id,
                )
                self._log(
                    "event_unhandled_payload",
                    level="WARNING",
                    message="TASK_COMPLETED task not found",
                    data={"issue_number": issue_number, "task_id": task_id},
                )
                return

            task = matching[0]
            if task.status is TaskState.COMPLETE:
                self._log(
                    "task_already_complete",
                    issue_number=task.issue_number,
                    message="TASK_COMPLETED event received for already complete task",
                )
                return

            if task.status is TaskState.READY_FOR_REVIEW:
                updated = task.transition(
                    TaskState.COMPLETE, current_agent=task.current_agent, now=current
                )
                self.queue = self.queue.replace(updated)
                self._release_dependencies(current)
                self._persist()
                self._log(
                    "task_transition",
                    issue_number=task.issue_number,
                    agent=task.current_agent,
                    task_id=task.task_id,
                    message="completed via TASK_COMPLETED event",
                    data={
                        "from_state": task.status.value,
                        "to_state": TaskState.COMPLETE.value,
                    },
                )
            else:
                logger.warning(
                    "TASK_COMPLETED cannot transition task #%d from %s to COMPLETE",
                    task.issue_number,
                    task.status.value,
                )
                self._log(
                    "event_invalid_transition",
                    level="WARNING",
                    issue_number=task.issue_number,
                    message=f"TASK_COMPLETED cannot transition task from {task.status.value}",
                    data={"from_state": task.status.value},
                )
        else:
            logger.warning("Unhandled or unsupported event type: %s", event.event_type)
            self._log(
                "event_unsupported",
                level="WARNING",
                message=f"unhandled or unsupported event type: {event.event_type}",
                data={"event_type": str(event.event_type), "payload": event.payload},
            )

    def tick(
        self,
        capacities: Iterable[Capacity] = (),
        *,
        now: datetime | None = None,
    ) -> bool:
        current = now or self.clock.now()

        event_capacities: dict[str, Capacity] = {}
        for source in self.event_sources:
            for event in source.poll(current):
                self._handle_event(event, current, event_capacities)

        # Pause stops new dispatch only. Existing PR CI remains observable so a bounded
        # --watch run can finish tracking already-running work while paused (#166).
        self._poll_ci_checks(current)

        if self.store.is_paused():
            return False

        self._expire_overrun_tasks(current)

        supplied = {capacity.agent: capacity for capacity in capacities}
        supplied.update(event_capacities)
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
                try:
                    ctx = self.worktree_adapter.prepare_worktree(task.issue_number)
                except WorktreeError as error:
                    reason = f"worktree preparation failed: {type(error).__name__}"
                    worktree_escalated = task.transition(
                        TaskState.NEEDS_HUMAN,
                        current_agent=None,
                        now=current,
                        reason=reason,
                    )
                    self.queue = self.queue.replace(worktree_escalated)
                    self._persist()
                    self._log(
                        "task_transition",
                        level="ERROR",
                        issue_number=worktree_escalated.issue_number,
                        agent=agent,
                        task_id=worktree_escalated.task_id,
                        message=reason,
                        data={
                            "from_state": task.status.value,
                            "to_state": worktree_escalated.status.value,
                            "attempt": worktree_escalated.attempt,
                        },
                    )
                    self._backoff_step = 0
                    return True
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
            # #164: record which process is executing this dispatch *before* calling
            # the (synchronous, blocking) worker, so that if this Scheduler process
            # itself dies mid-dispatch, a future restart can tell the task was really
            # in flight and not just abandoned. Cleared unconditionally once the worker
            # call returns, whether it succeeded or raised.
            if running.worktree is not None:
                save_process_record(
                    Path(running.worktree),
                    ProcessRecord(
                        pid=os.getpid(),
                        started_at=get_process_start_time(os.getpid()) or "",
                        agent=agent,
                        issue_number=running.issue_number,
                        worktree=running.worktree,
                        attempt_nonce=lease.nonce,
                    ),
                )
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
            finally:
                if running.worktree is not None:
                    clear_process_record(Path(running.worktree), running.issue_number)

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

            self._handle_result(running, agent, result, current, effective_capacities=effective)
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
                existing_cap = self._cooldowns.get(agent)
                if existing_cap is None or existing_cap.state not in {
                    CapacityState.AUTH_ERROR,
                    CapacityState.DISABLED_BILLING,
                }:
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
        base_branch = self.base_branch
        if base_branch is None:
            return verifying.transition(
                TaskState.NEEDS_HUMAN,
                current_agent=agent,
                now=now,
                reason="base branch is unresolved; failing closed before git operations",
            )

        rebase_result = rebase_onto_base(worktree_dir, base_branch=base_branch)
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

        # #202: rerun verification gates on the post-rebase tree before push/PR,
        # and record checkpoint evidence for the resulting commit SHA.
        from subsched.checkpoint import capture_mechanical_checkpoint, save_checkpoint
        from subsched.verification import run_verification

        self._log(
            "verification_start",
            issue_number=verifying.issue_number,
            agent=agent,
            task_id=verifying.task_id,
            data={
                "commands": len(self.verification_commands),
                "attempt": verifying.attempt,
                "post_rebase": True,
            },
        )
        post_rebase_report = run_verification(
            worktree_dir,
            self.verification_commands,
            timeout_seconds=self.verification_timeout_seconds,
        )
        self._log(
            "gate_result",
            issue_number=verifying.issue_number,
            agent=agent,
            task_id=verifying.task_id,
            data={
                "passed": post_rebase_report.passed,
                "attempt": verifying.attempt,
                "post_rebase": True,
            },
        )
        result_kind = (
            AgentResultKind.PASS if post_rebase_report.passed else AgentResultKind.FAILURE
        )
        cp = capture_mechanical_checkpoint(
            worktree_dir,
            verifying.issue_number,
            AgentResult(result_kind),
            exit_code=0 if post_rebase_report.passed else 1,
            test_results=post_rebase_report.summary,
        )
        save_checkpoint(worktree_dir, cp)

        if not post_rebase_report.passed:
            new_verification_failures = verifying.verification_failures + 1
            retry = verifying.transition(
                TaskState.RETRY,
                current_agent=None,
                increment_attempt=True,
                now=now,
                reason=f"post-rebase verification failed: {post_rebase_report.summary}",
            )
            retry = replace(retry, verification_failures=new_verification_failures)
            next_state = (
                TaskState.NEEDS_HUMAN
                if new_verification_failures >= self.max_verification_failures
                else TaskState.READY
            )
            return retry.transition(next_state, now=now)

        verification_summary = post_rebase_report.summary


        # #140: never push a commit whose message contains a GitHub auto-close keyword
        # (Fixes/Closes/Resolves #N) -- that would let a merge auto-close the issue,
        # bypassing the "issues stay open until manual review" invariant. This never
        # rewrites history; it only inspects and, on any violation (including an
        # inconclusive git failure), fails closed to NEEDS_HUMAN instead of pushing.
        remote_base_ref = f"refs/remotes/origin/{base_branch}"
        violations = find_close_keyword_commits(worktree_dir, remote_base_ref)
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
            base=base_branch,
            repo=self.repo,
            verification_summary=verification_summary,
            close_issue=self.close_issue_enabled,
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
            AgentResultKind.AUTH_ERROR,
            AgentResultKind.BILLING_ERROR,
            AgentResultKind.UNKNOWN_BILLING,
            AgentResultKind.PERMISSION_DENIED,
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

    def _has_available_alternative_agent(
        self,
        exclude_agent: str,
        effective_capacities: dict[str, Capacity] | None,
        now: datetime,
    ) -> bool:
        if effective_capacities is not None:
            candidates = [
                c
                for c in effective_capacities.values()
                if c.agent != exclude_agent and not self.lease_manager.is_agent_busy(c.agent)
            ]
            return self.router.select(candidates, now=now) is not None
        candidates = [
            c
            for c in self._cooldowns.values()
            if c.agent != exclude_agent and not self.lease_manager.is_agent_busy(c.agent)
        ]
        return self.router.select(candidates, now=now) is not None

    def _handle_result(
        self,
        task: Task,
        agent: str,
        result: AgentResult,
        now: datetime,
        *,
        effective_capacities: dict[str, Capacity] | None = None,
    ) -> None:
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
                # #175: verification-gate failures are their own budget
                # (max_verification_failures), never mixed with per-agent Agent
                # failure counts -- this branch does not touch per_agent_failures.
                new_verification_failures = task.verification_failures + 1
                retry = verifying.transition(
                    TaskState.RETRY, current_agent=None, increment_attempt=True, now=now
                )
                retry = replace(retry, verification_failures=new_verification_failures)
                next_state = (
                    TaskState.NEEDS_HUMAN
                    if new_verification_failures >= self.max_verification_failures
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
        elif result.kind in {
            AgentResultKind.CAPACITY_SESSION,
            AgentResultKind.CAPACITY_WEEKLY,
            AgentResultKind.CAPACITY_TEMPORARY,
        }:
            if result.kind is AgentResultKind.CAPACITY_SESSION:
                state = CapacityState.COOLDOWN_SESSION
            elif result.kind is AgentResultKind.CAPACITY_WEEKLY:
                state = CapacityState.COOLDOWN_WEEKLY
            else:
                state = CapacityState.RATE_LIMITED_TEMPORARY

            # #174: use explicit reset_at or bounded backoff (60s) for temporary capacity
            reset_at = result.reset_at or (
                now + timedelta(seconds=60)
                if state is CapacityState.RATE_LIMITED_TEMPORARY
                else None
            )
            from subsched.capacity.base import BLOCKER_SEVERITY

            existing_cap = self._cooldowns.get(agent)
            should_update_cooldown = (
                existing_cap is None
                or BLOCKER_SEVERITY.get(state, 0) >= BLOCKER_SEVERITY.get(existing_cap.state, 0)
            )
            if should_update_cooldown:
                self._cooldowns = {
                    **self._cooldowns,
                    agent: Capacity(
                        agent=agent,
                        state=state,
                        reset_at=reset_at,
                        observed_at=now,
                        source="structured_result",
                        confidence="high",
                    ),
                }
            new_capacity_events = task.capacity_events + 1
            # #174: CAPACITY_TEMPORARY is a transient saturation event rather than
            # a session/weekly quota cutoff, so it does not increment agent_switches
            # and does not escalate to NEEDS_HUMAN on repeated occurrences.
            new_agent_switches = (
                task.agent_switches
                if result.kind is AgentResultKind.CAPACITY_TEMPORARY
                else task.agent_switches + 1
            )
            switched = replace(
                task,
                agent_switches=new_agent_switches,
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
        elif result.kind is AgentResultKind.PERMISSION_DENIED:
            reason = f"permission denied: {result.output or 'agent reported PERMISSION_DENIED'}"
            final = task.transition(
                TaskState.NEEDS_HUMAN, current_agent=None, now=now, reason=reason
            )
            self.queue = self.queue.replace(final)
            self._log(
                "task_transition",
                level="ERROR",
                issue_number=final.issue_number,
                agent=agent,
                task_id=final.task_id,
                message=reason,
                data={
                    "from_state": task.status.value,
                    "to_state": final.status.value,
                    "attempt": final.attempt,
                    "result_kind": result.kind.value,
                },
            )
        elif result.kind in {
            AgentResultKind.AUTH_ERROR,
            AgentResultKind.BILLING_ERROR,
            AgentResultKind.UNKNOWN_BILLING,
        }:
            target_state = (
                CapacityState.AUTH_ERROR
                if result.kind is AgentResultKind.AUTH_ERROR
                else CapacityState.DISABLED_BILLING
            )
            from subsched.capacity.base import BLOCKER_SEVERITY

            existing_cap = self._cooldowns.get(agent)
            should_update_cooldown = (
                existing_cap is None
                or BLOCKER_SEVERITY.get(target_state, 0)
                >= BLOCKER_SEVERITY.get(existing_cap.state, 0)
            )
            if should_update_cooldown:
                self._cooldowns = {
                    **self._cooldowns,
                    agent: Capacity(
                        agent=agent,
                        state=target_state,
                        reset_at=None,
                        observed_at=now,
                        source="structured_result",
                        confidence="high",
                    ),
                }
            if self._has_available_alternative_agent(
                exclude_agent=agent,
                effective_capacities=effective_capacities,
                now=now,
            ):
                self.queue = self.queue.requeue_after_capacity_event(task.issue_number)
                self._log(
                    "task_transition",
                    level="WARN",
                    issue_number=task.issue_number,
                    agent=agent,
                    task_id=task.task_id,
                    data={
                        "from_state": task.status.value,
                        "to_state": TaskState.READY.value,
                        "capacity_state": target_state.value,
                        "failover": True,
                        "attempt": task.attempt,
                    },
                )
            else:
                error_label = (
                    "auth error"
                    if result.kind is AgentResultKind.AUTH_ERROR
                    else "billing error"
                )
                reason = f"{error_label} for agent '{agent}': no alternative agent available"
                final = task.transition(
                    TaskState.NEEDS_HUMAN,
                    current_agent=None,
                    now=now,
                    reason=reason,
                )
                self.queue = self.queue.replace(final)
                self._log(
                    "task_transition",
                    level="ERROR",
                    issue_number=final.issue_number,
                    agent=agent,
                    task_id=final.task_id,
                    message=reason,
                    data={
                        "from_state": task.status.value,
                        "to_state": final.status.value,
                        "capacity_state": target_state.value,
                        "failover": False,
                        "attempt": final.attempt,
                    },
                )
        else:
            failures_dict = dict(task.per_agent_failures)
            failures_dict[agent] = failures_dict.get(agent, 0) + 1
            # #175: escalation is gated on this Agent's own failure count, not on
            # task.attempt -- task.attempt also advances on verification retries, which
            # must not count against max_agent_failures (docs/SPEC.md #50).
            agent_failure_count = failures_dict[agent]
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
                if agent_failure_count >= self.max_agent_failures
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
