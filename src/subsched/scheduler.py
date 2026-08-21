from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from subsched.models import (
    AgentResult,
    AgentResultKind,
    Capacity,
    CapacityState,
    Issue,
    Task,
    TaskState,
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
        label_scores: dict[str, int] | None = None,
        max_agent_failures: int = 2,
        max_agent_switches: int = 6,
        max_tasks: int = 50,
    ) -> None:
        self.store = store
        self.router = router
        self.worker = worker
        self.worktree_root = worktree_root.resolve()
        self.worktree_adapter = worktree_adapter
        self.queue = TaskQueue(store.load_tasks(), label_scores=label_scores)
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

    @property
    def tasks(self) -> tuple[Task, ...]:
        return self.queue.tasks

    def discover(
        self, issues: Iterable[Issue], *, exclude_labels: frozenset[str] = frozenset()
    ) -> None:
        existing = {task.issue_number for task in self.tasks}
        additions = tuple(
            Task.from_issue(issue)
            for issue in issues
            if issue.number not in existing and not exclude_labels.intersection(issue.labels)
        )
        if len(self.tasks) + len(additions) > self.max_tasks:
            raise ValueError(f"task limit exceeded ({self.max_tasks})")
        self.queue = self.queue.append(additions)
        self._persist()

    def run_until_waiting(
        self,
        capacities: Iterable[Capacity],
        *,
        now: datetime | None = None,
    ) -> None:
        current = now or datetime.now(UTC)
        if self.store.is_paused():
            return
        supplied = {capacity.agent: capacity for capacity in capacities}
        effective = self._effective_capacities(supplied, current)
        if self.router.select(effective.values(), now=current) is not None:
            self._release_waiting_tasks(current)
        self._release_dependencies(current)
        self.is_waiting_for_capacity = any(
            task.status is TaskState.WAITING_CAPACITY for task in self.tasks
        )

        while self.queue.ready():
            if self.store.is_paused():
                return
            agent = self.router.select(effective.values(), now=current)
            if agent is None:
                task = self.queue.ready()[0]
                self.queue = self.queue.replace(task.transition(TaskState.WAITING_CAPACITY))
                self.is_waiting_for_capacity = True
                self._persist()
                return

            task = self.queue.ready()[0]
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
            dispatched = task.transition(TaskState.DISPATCHED, current_agent=agent, now=current)
            running = dispatched.transition(TaskState.IN_PROGRESS, current_agent=agent, now=current)
            self.queue = self.queue.replace(running)
            self._persist()
            try:
                result = self.worker.run(running, agent)
            except Exception as error:
                result = AgentResult(AgentResultKind.FAILURE, output=type(error).__name__)
            self._handle_result(running, agent, result, current)
            effective = self._effective_capacities(supplied, current)
            self._release_dependencies(current)

    def _handle_result(self, task: Task, agent: str, result: AgentResult, now: datetime) -> None:
        if result.kind is AgentResultKind.PASS:
            verifying = task.transition(TaskState.VERIFYING, current_agent=agent, now=now)
            pr_ready = verifying.transition(TaskState.PR_READY, current_agent=agent, now=now)
            ready_for_review = pr_ready.transition(
                TaskState.READY_FOR_REVIEW, current_agent=agent, now=now
            )
            complete = ready_for_review.transition(
                TaskState.COMPLETE, current_agent=agent, now=now
            )
            self.queue = self.queue.replace(complete)
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
            switched = replace(task, agent_switches=task.agent_switches + 1)
            self.queue = self.queue.replace(switched)
            if switched.agent_switches >= self.max_agent_switches:
                waiting = switched.transition(TaskState.WAITING_CAPACITY, current_agent=None)
                self.queue = self.queue.replace(
                    waiting.transition(TaskState.NEEDS_HUMAN, current_agent=None)
                )
            else:
                self.queue = self.queue.requeue_after_capacity_event(task.issue_number)
        else:
            retry = task.transition(
                TaskState.RETRY,
                current_agent=None,
                increment_attempt=True,
                now=now,
            )
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
        for task in self.tasks:
            if task.status is TaskState.WAITING_DEPENDENCY and set(task.dependencies) <= completed:
                self.queue = self.queue.replace(task.transition(TaskState.READY, now=now))

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
