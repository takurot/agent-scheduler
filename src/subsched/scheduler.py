from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from subsched.contract import bootstrap_task_files
from subsched.events import Clock, EventSource, EventType, SystemClock
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
        verification_commands: tuple[str, ...] = ('pytest -q', 'ruff check .'),
        label_scores: dict[str, int] | None = None,
        concurrency: int = 1,
        max_agent_failures: int = 2,
        max_agent_switches: int = 6,
        max_tasks: int = 50,
    ) -> None:
        self.store = store
        self.router = router
        self.worker = worker
        self.worktree_root = worktree_root.resolve()
        self.worktree_adapter = worktree_adapter
        self.clock = clock or SystemClock()
        self.event_sources = event_sources
        self.verification_commands = verification_commands
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

    def discover(
        self, issues: Iterable[Issue], *, exclude_labels: frozenset[str] = frozenset()
    ) -> None:
        effective_exclude = frozenset({"security-sensitive"}).union(exclude_labels)
        existing = {task.issue_number for task in self.tasks}
        additions = tuple(
            Task.from_issue(issue)
            for issue in issues
            if issue.number not in existing and not effective_exclude.intersection(issue.labels)
        )
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
            return False

        available_capacities = [
            c for c in effective.values() if not self.lease_manager.is_agent_busy(c.agent)
        ]
        agent = self.router.select(available_capacities, now=current)
        if agent is None:
            task = ready_tasks[0]
            self.queue = self.queue.replace(task.transition(TaskState.WAITING_CAPACITY))
            self.is_waiting_for_capacity = True
            self._persist()
            return False

        task = ready_tasks[0]
        lease = self.lease_manager.acquire(task.issue_number, agent, now=current)
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
            bootstrap_task_files(Path(task.worktree), task)

        actual_switches = task.actual_agent_switches
        if task.last_dispatched_agent is not None and task.last_dispatched_agent != agent:
            actual_switches += 1
        dispatched = task.transition(TaskState.DISPATCHED, current_agent=agent, now=current)
        dispatched = replace(
            dispatched,
            actual_agent_switches=actual_switches,
            last_dispatched_agent=agent,
        )
        running = dispatched.transition(TaskState.IN_PROGRESS, current_agent=agent, now=current)
        self.queue = self.queue.replace(running)
        self._persist()
        try:
            try:
                result = self.worker.run(running, agent)
            except Exception as error:
                result = AgentResult(AgentResultKind.FAILURE, output=type(error).__name__)
            self._handle_result(running, agent, result, current)
        finally:
            self.lease_manager.release(task.issue_number, nonce=lease.nonce)
        self._effective_capacities(supplied, current)
        self._release_dependencies(current)
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

    def _handle_result(self, task: Task, agent: str, result: AgentResult, now: datetime) -> None:
        if result.kind is AgentResultKind.PASS:
            verifying = task.transition(TaskState.VERIFYING, current_agent=agent, now=now)
            self.queue = self.queue.replace(verifying)
            self._persist()

            verification_ok = True
            if task.worktree is not None:
                from subsched.checkpoint import capture_mechanical_checkpoint, save_checkpoint
                from subsched.verification import run_verification
                v_report = run_verification(Path(task.worktree), ("true",))
                verification_ok = v_report.passed
                cp = capture_mechanical_checkpoint(
                    Path(task.worktree),
                    task.issue_number,
                    result,
                    exit_code=0 if verification_ok else 1,
                    test_results=v_report.summary,
                )
                save_checkpoint(Path(task.worktree), cp)

            if verification_ok:
                pr_ready = verifying.transition(TaskState.PR_READY, current_agent=agent, now=now)
                ready_for_review = pr_ready.transition(
                    TaskState.READY_FOR_REVIEW, current_agent=agent, now=now
                )
                complete = ready_for_review.transition(
                    TaskState.COMPLETE, current_agent=agent, now=now
                )
                self.queue = self.queue.replace(complete)
            else:
                retry = verifying.transition(
                    TaskState.RETRY, current_agent=None, increment_attempt=True, now=now
                )
                next_state = (
                    TaskState.NEEDS_HUMAN
                    if retry.attempt >= self.max_agent_failures
                    else TaskState.READY
                )
                self.queue = self.queue.replace(retry.transition(next_state, now=now))
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
                self.queue = self.queue.replace(
                    waiting.transition(TaskState.NEEDS_HUMAN, current_agent=None)
                )
            else:
                self.queue = self.queue.requeue_after_capacity_event(task.issue_number)
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
            self.queue = self.queue.replace(retry.transition(next_state, now=now))
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
        self.store.save_state(
            self.tasks,
            paused=self.store.is_paused(),
            capacities=self._cooldowns.values(),
        )
