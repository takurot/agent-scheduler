from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from subsched.models import Task, TaskState


@dataclass(frozen=True, slots=True)
class CapacityMetrics:
    capacity_exhaustion_events: int
    failover_attempts: int
    successful_continuations: int
    excluded_events: int
    failover_success_rate: float | None
    exclusion_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "capacity_exhaustion_events": self.capacity_exhaustion_events,
            "failover_attempts": self.failover_attempts,
            "successful_continuations": self.successful_continuations,
            "excluded_events": self.excluded_events,
            "failover_success_rate": self.failover_success_rate,
            "exclusion_reasons": list(self.exclusion_reasons),
        }


def calculate_capacity_metrics(tasks: Iterable[Task]) -> CapacityMetrics:
    task_list = tuple(tasks)
    total_exhaustion = 0
    failover_attempts = 0
    successful_continuations = 0
    excluded_events = 0
    exclusion_reasons: list[str] = []

    for task in task_list:
        if task.capacity_events <= 0:
            continue
        total_exhaustion += task.capacity_events

        if task.status is TaskState.CANCELLED:
            excluded_events += task.capacity_events
            exclusion_reasons.append(
                f"Task #{task.issue_number} was cancelled before completing failover"
            )
            continue

        failover_attempts += task.capacity_events

        if task.status in {
            TaskState.COMPLETE,
            TaskState.READY_FOR_REVIEW,
            TaskState.PR_READY,
            TaskState.VERIFYING,
            TaskState.IN_PROGRESS,
            TaskState.DISPATCHED,
            TaskState.READY,
            TaskState.WAITING_CAPACITY,
            TaskState.WAITING_DEPENDENCY,
        }:
            successful_continuations += task.capacity_events
        elif task.status is TaskState.NEEDS_HUMAN or task.status is TaskState.FAILED:
            # Did not successfully continue
            pass
        else:
            pass

    rate = (
        round(successful_continuations / failover_attempts, 4)
        if failover_attempts > 0
        else None
    )

    return CapacityMetrics(
        capacity_exhaustion_events=total_exhaustion,
        failover_attempts=failover_attempts,
        successful_continuations=successful_continuations,
        excluded_events=excluded_events,
        failover_success_rate=rate,
        exclusion_reasons=tuple(exclusion_reasons),
    )


def format_capacity_report(metrics: CapacityMetrics) -> str:
    lines = [
        "Capacity Failover Metrics Report",
        "--------------------------------",
        f"Capacity Exhaustion Events: {metrics.capacity_exhaustion_events}",
        f"Failover Attempts: {metrics.failover_attempts}",
        f"Successful Continuations: {metrics.successful_continuations}",
        f"Excluded Events: {metrics.excluded_events}",
    ]
    if metrics.failover_success_rate is not None:
        lines.append(f"Failover Success Rate: {metrics.failover_success_rate * 100:.1f}%")
    else:
        lines.append("Failover Success Rate: N/A (no events)")

    if metrics.exclusion_reasons:
        lines.append("\nExclusion Reasons:")
        for r in metrics.exclusion_reasons:
            lines.append(f"  - {r}")

    return "\n".join(lines)
