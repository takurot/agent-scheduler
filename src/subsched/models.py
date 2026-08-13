from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class TaskState(StrEnum):
    DISCOVERED = "DISCOVERED"
    READY = "READY"
    DISPATCHED = "DISPATCHED"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    RETRY = "RETRY"
    WAITING_CAPACITY = "WAITING_CAPACITY"
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    BLOCKED = "BLOCKED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    COMPLETE = "COMPLETE"


class CapacityState(StrEnum):
    AVAILABLE = "AVAILABLE"
    BUSY = "BUSY"
    PRESSURED_SESSION = "PRESSURED_SESSION"
    PRESSURED_WEEKLY = "PRESSURED_WEEKLY"
    COOLDOWN_SESSION = "COOLDOWN_SESSION"
    COOLDOWN_WEEKLY = "COOLDOWN_WEEKLY"
    COOLDOWN_MODEL = "COOLDOWN_MODEL"
    RATE_LIMITED_TEMPORARY = "RATE_LIMITED_TEMPORARY"
    AUTH_ERROR = "AUTH_ERROR"
    DISABLED_BILLING = "DISABLED_BILLING"
    DISABLED = "DISABLED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class AgentResultKind(StrEnum):
    PASS = "PASS"
    CAPACITY_SESSION = "CAPACITY_SESSION"
    CAPACITY_WEEKLY = "CAPACITY_WEEKLY"
    CAPACITY_TEMPORARY = "CAPACITY_TEMPORARY"
    AUTH_ERROR = "AUTH_ERROR"
    BILLING_ERROR = "BILLING_ERROR"
    UNKNOWN_BILLING = "UNKNOWN_BILLING"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TIMEOUT = "TIMEOUT"
    PROCESS_CLEANUP_FAILED = "PROCESS_CLEANUP_FAILED"
    UNKNOWN = "UNKNOWN"
    FAILURE = "FAILURE"


class StateTransitionError(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.DISCOVERED: frozenset({TaskState.READY, TaskState.WAITING_DEPENDENCY}),
    TaskState.READY: frozenset(
        {TaskState.DISPATCHED, TaskState.WAITING_CAPACITY, TaskState.CANCELLED}
    ),
    TaskState.DISPATCHED: frozenset({TaskState.IN_PROGRESS, TaskState.RETRY, TaskState.CANCELLED}),
    TaskState.IN_PROGRESS: frozenset(
        {
            TaskState.COMPLETE,
            TaskState.RETRY,
            TaskState.WAITING_CAPACITY,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.RETRY: frozenset({TaskState.READY, TaskState.NEEDS_HUMAN, TaskState.FAILED}),
    TaskState.WAITING_CAPACITY: frozenset(
        {TaskState.READY, TaskState.NEEDS_HUMAN, TaskState.CANCELLED}
    ),
    TaskState.WAITING_DEPENDENCY: frozenset(
        {TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED}
    ),
    TaskState.VERIFYING: frozenset({TaskState.COMPLETE, TaskState.RETRY}),
    TaskState.BLOCKED: frozenset({TaskState.READY, TaskState.CANCELLED}),
    TaskState.NEEDS_HUMAN: frozenset({TaskState.READY, TaskState.CANCELLED}),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.COMPLETE: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Issue:
    number: int
    title: str
    body: str = ""
    labels: tuple[str, ...] = ()
    url: str | None = None

    def __post_init__(self) -> None:
        if self.number <= 0:
            raise ValueError("issue number must be positive")
        if not self.title.strip():
            raise ValueError("issue title must not be empty")


def parse_dependencies(body: str) -> tuple[int, ...]:
    matches = re.findall(r"(?im)^\s*Blocked-By\s*:\s*(.+)$", body)
    numbers = {int(number) for line in matches for number in re.findall(r"#(\d+)", line)}
    return tuple(sorted(numbers))


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    issue_number: int
    title: str
    labels: tuple[str, ...]
    status: TaskState
    attempt: int = 0
    agent_switches: int = 0
    current_agent: str | None = None
    worktree: str | None = None
    dependencies: tuple[int, ...] = ()
    pr: int | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_issue(cls, issue: Issue, *, worktree: str | None = None) -> Task:
        dependencies = parse_dependencies(issue.body)
        initial = TaskState.WAITING_DEPENDENCY if dependencies else TaskState.READY
        return cls(
            task_id=f"github-{issue.number}",
            issue_number=issue.number,
            title=issue.title,
            labels=issue.labels,
            status=initial,
            worktree=worktree,
            dependencies=dependencies,
        )

    def transition(
        self,
        status: TaskState,
        *,
        current_agent: str | None = None,
        increment_attempt: bool = False,
        now: datetime | None = None,
    ) -> Task:
        if status not in ALLOWED_TRANSITIONS[self.status]:
            raise StateTransitionError(f"invalid transition: {self.status} -> {status}")
        return replace(
            self,
            status=status,
            current_agent=current_agent,
            attempt=self.attempt + int(increment_attempt),
            updated_at=now or datetime.now(UTC),
        )

    def with_worktree(self, worktree: str) -> Task:
        return replace(self, worktree=worktree)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "issue_number": self.issue_number,
            "title": self.title,
            "labels": list(self.labels),
            "status": self.status.value,
            "attempt": self.attempt,
            "agent_switches": self.agent_switches,
            "current_agent": self.current_agent,
            "worktree": self.worktree,
            "dependencies": list(self.dependencies),
            "pr": self.pr,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Task:
        try:
            updated = value.get("updated_at")
            return cls(
                task_id=str(value["task_id"]),
                issue_number=int(value["issue_number"]),
                title=str(value["title"]),
                labels=tuple(str(label) for label in value["labels"]),
                status=TaskState(value["status"]),
                attempt=int(value["attempt"]),
                agent_switches=int(value.get("agent_switches", 0)),
                current_agent=value.get("current_agent"),
                worktree=value.get("worktree"),
                dependencies=tuple(int(item) for item in value["dependencies"]),
                pr=value.get("pr"),
                updated_at=datetime.fromisoformat(updated) if updated else None,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid task state") from error


@dataclass(frozen=True, slots=True)
class Capacity:
    agent: str
    state: CapacityState
    observed_at: datetime
    source: str
    confidence: str
    scope: str | None = None
    used_percentage: float | None = None
    reset_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.used_percentage is not None and not 0 <= self.used_percentage <= 100:
            raise ValueError("used_percentage must be between 0 and 100")

    @property
    def remaining_percentage(self) -> float | None:
        return None if self.used_percentage is None else 100 - self.used_percentage

    def is_available(self, now: datetime) -> bool:
        return self.state is CapacityState.AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "state": self.state.value,
            "observed_at": self.observed_at.isoformat(),
            "source": self.source,
            "confidence": self.confidence,
            "scope": self.scope,
            "used_percentage": self.used_percentage,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Capacity:
        try:
            reset_at = value.get("reset_at")
            return cls(
                agent=str(value["agent"]),
                state=CapacityState(value["state"]),
                observed_at=datetime.fromisoformat(value["observed_at"]),
                source=str(value["source"]),
                confidence=str(value["confidence"]),
                scope=value.get("scope"),
                used_percentage=value.get("used_percentage"),
                reset_at=datetime.fromisoformat(reset_at) if reset_at else None,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid capacity state") from error


@dataclass(frozen=True, slots=True)
class AgentResult:
    kind: AgentResultKind
    reset_at: datetime | None = None
    output: str = ""

    def __post_init__(self) -> None:
        if (
            self.kind in {AgentResultKind.CAPACITY_SESSION, AgentResultKind.CAPACITY_WEEKLY}
            and self.reset_at is None
        ):
            raise ValueError("reset_at is required for capacity events")
