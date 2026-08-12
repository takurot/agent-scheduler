from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from subsched.models import Task, TaskState


@dataclass(frozen=True, slots=True)
class TaskQueue:
    tasks: tuple[Task, ...] = ()
    label_scores: dict[str, int] | None = None
    front_issue: int | None = None

    def __post_init__(self) -> None:
        issues = [task.issue_number for task in self.tasks]
        if len(issues) != len(set(issues)):
            raise ValueError("duplicate active task for issue")

    def ready(self) -> tuple[Task, ...]:
        candidates = (task for task in self.tasks if task.status is TaskState.READY)
        return tuple(sorted(candidates, key=self._sort_key))

    def _sort_key(self, task: Task) -> tuple[int, int, int]:
        scores = self.label_scores or {}
        score = max((scores.get(label, 0) for label in task.labels), default=0)
        front = 0 if task.issue_number == self.front_issue else 1
        return (front, -score, task.issue_number)

    def replace(self, replacement: Task) -> TaskQueue:
        if not any(task.issue_number == replacement.issue_number for task in self.tasks):
            raise KeyError(replacement.issue_number)
        updated = tuple(
            replacement if task.issue_number == replacement.issue_number else task
            for task in self.tasks
        )
        return replace(self, tasks=updated)

    def append(self, additions: Iterable[Task]) -> TaskQueue:
        return replace(self, tasks=(*self.tasks, *tuple(additions)))

    def requeue_after_capacity_event(self, issue_number: int) -> TaskQueue:
        task = self.get(issue_number)
        waiting = task.transition(TaskState.WAITING_CAPACITY, current_agent=None)
        ready = waiting.transition(TaskState.READY, current_agent=None)
        return replace(self.replace(ready), front_issue=issue_number)

    def get(self, issue_number: int) -> Task:
        for task in self.tasks:
            if task.issue_number == issue_number:
                return task
        raise KeyError(issue_number)
