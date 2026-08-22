from __future__ import annotations

import secrets
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from subsched.models import Task, TaskState


class LeaseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TaskLease:
    issue_number: int
    agent: str
    acquired_at: datetime
    nonce: str


class LeaseManager:
    """Manages concurrency-safe task leases and agent busy reservations."""

    def __init__(self, max_concurrency: int = 1) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        self.max_concurrency = max_concurrency
        self._lock = threading.Lock()
        self._leases: dict[int, TaskLease] = {}
        self._busy_agents: dict[str, int] = {}

    def acquire(
        self,
        issue_number: int,
        agent: str,
        *,
        now: datetime | None = None,
    ) -> TaskLease:
        with self._lock:
            if len(self._leases) >= self.max_concurrency:
                raise LeaseError(
                    f"concurrency limit reached ({len(self._leases)}/{self.max_concurrency})"
                )
            if issue_number in self._leases:
                raise LeaseError(f"issue #{issue_number} is already leased")
            if agent in self._busy_agents:
                busy_issue = self._busy_agents[agent]
                raise LeaseError(f"agent '{agent}' is already busy on issue #{busy_issue}")

            current = now or datetime.now(UTC)
            nonce = secrets.token_hex(8)
            lease = TaskLease(
                issue_number=issue_number,
                agent=agent,
                acquired_at=current,
                nonce=nonce,
            )
            self._leases[issue_number] = lease
            self._busy_agents[agent] = issue_number
            return lease

    def release(self, issue_number: int, *, nonce: str | None = None) -> None:
        with self._lock:
            lease = self._leases.get(issue_number)
            if lease is None:
                return
            if nonce is not None and lease.nonce != nonce:
                return
            del self._leases[issue_number]
            self._busy_agents.pop(lease.agent, None)

    def reconcile(self, tasks: Iterable[Task]) -> None:
        with self._lock:
            active_tasks = {
                t.issue_number: t
                for t in tasks
                if t.status in {TaskState.DISPATCHED, TaskState.IN_PROGRESS}
            }
            # Remove stale leases
            stale_issues = [num for num in self._leases if num not in active_tasks]
            for num in stale_issues:
                lease = self._leases.pop(num)
                self._busy_agents.pop(lease.agent, None)

            # Ensure active tasks have registered leases
            for num, task in active_tasks.items():
                if num not in self._leases and task.current_agent:
                    nonce = secrets.token_hex(8)
                    lease = TaskLease(
                        issue_number=num,
                        agent=task.current_agent,
                        acquired_at=task.updated_at or datetime.now(UTC),
                        nonce=nonce,
                    )
                    self._leases[num] = lease
                    self._busy_agents[task.current_agent] = num

    def is_agent_busy(self, agent: str) -> bool:
        with self._lock:
            return agent in self._busy_agents

    def is_task_leased(self, issue_number: int) -> bool:
        with self._lock:
            return issue_number in self._leases

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._leases)
