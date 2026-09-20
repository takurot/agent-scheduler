from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from subsched.capacity.metrics import (
    CapacityMetrics,
    calculate_capacity_metrics,
    format_capacity_report,
)
from subsched.models import Task, TaskState


@dataclass(frozen=True, slots=True)
class ProductivityMetrics:
    issues_attempted: int
    issues_implemented: int
    prs_created: int
    autonomous_completion_rate: float | None
    issues_ready_for_review: int = 0
    # #378: how many of `issues_attempted` were inferred from legacy state that lacks
    # `run_started_at`, as opposed to confirmed by a recorded first dispatch.
    issues_attempted_inferred: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReliabilityMetrics:
    task_completion_rate: float | None
    manual_intervention_rate: float | None
    agent_failure_switch_rate: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SchedulerMetrics:
    productivity: ProductivityMetrics
    reliability: ReliabilityMetrics
    capacity: CapacityMetrics

    def to_dict(self) -> dict[str, Any]:
        return {
            "productivity": self.productivity.to_dict(),
            "reliability": self.reliability.to_dict(),
            "capacity": self.capacity.to_dict(),
        }


# #378: states reachable only after a Task has been dispatched at least once. Used solely
# to infer an attempt for legacy state that predates `run_started_at` (#137).
_POST_DISPATCH_STATES = frozenset(
    {
        TaskState.DISPATCHED,
        TaskState.PLANNING,
        TaskState.PLAN_REVIEW,
        TaskState.IN_PROGRESS,
        TaskState.VERIFYING,
        TaskState.PR_READY,
        TaskState.PR_REVIEW,
        TaskState.REVISING,
        TaskState.READY_FOR_REVIEW,
        TaskState.NEEDS_REBASE,
    }
)


def _was_dispatched(task: Task) -> bool:
    """A Task is "attempted" once it has been dispatched -- not merely because its
    current status is past DISCOVERED (Task.from_issue() creates READY /
    WAITING_DEPENDENCY / BLOCKED tasks that never ran)."""
    return task.run_started_at is not None or _inferred_dispatch(task)


def _inferred_dispatch(task: Task) -> bool:
    if task.run_started_at is not None:
        return False
    return (
        task.attempt > 0
        or task.last_dispatched_agent is not None
        or task.capacity_events > 0
        or task.verification_failures > 0
        or bool(task.per_agent_failures)
        or task.pr is not None
        or task.status in _POST_DISPATCH_STATES
    )


def _failure_driven_switches(task: Task) -> int:
    """Switches attributable to Agent failures. No per-cause counter is persisted, so
    this is derived: every actual switch minus the capacity-driven ones
    (`agent_switches`), clamped to [0, total failures] for this Task."""
    failures = sum(cnt for _, cnt in task.per_agent_failures)
    return min(max(task.actual_agent_switches - task.agent_switches, 0), failures)


def calculate_metrics(tasks: Iterable[Task]) -> SchedulerMetrics:
    task_list = tuple(tasks)
    attempted = [t for t in task_list if _was_dispatched(t)]
    num_attempted = len(attempted)
    num_inferred = sum(1 for t in attempted if _inferred_dispatch(t))

    implemented = [
        t
        for t in attempted
        if t.status in {TaskState.READY_FOR_REVIEW, TaskState.COMPLETE, TaskState.PR_READY}
    ]
    num_implemented = len(implemented)
    num_ready_for_review = sum(1 for t in attempted if t.status is TaskState.READY_FOR_REVIEW)

    num_prs = sum(1 for t in task_list if t.pr is not None)
    auto_rate = (
        round(num_implemented / num_attempted, 4) if num_attempted > 0 else None
    )

    completed = [t for t in attempted if t.status is TaskState.COMPLETE]
    completion_rate = (
        round(len(completed) / num_attempted, 4) if num_attempted > 0 else None
    )

    human_needed = [t for t in attempted if t.status is TaskState.NEEDS_HUMAN]
    manual_rate = (
        round(len(human_needed) / num_attempted, 4) if num_attempted > 0 else None
    )

    total_failures = sum(
        sum(cnt for _, cnt in t.per_agent_failures) for t in task_list
    )
    total_switches = sum(_failure_driven_switches(t) for t in task_list)
    switch_rate = (
        round(total_switches / total_failures, 4) if total_failures > 0 else None
    )

    productivity = ProductivityMetrics(
        issues_attempted=num_attempted,
        issues_implemented=num_implemented,
        prs_created=num_prs,
        autonomous_completion_rate=auto_rate,
        issues_ready_for_review=num_ready_for_review,
        issues_attempted_inferred=num_inferred,
    )

    reliability = ReliabilityMetrics(
        task_completion_rate=completion_rate,
        manual_intervention_rate=manual_rate,
        agent_failure_switch_rate=switch_rate,
    )

    capacity = calculate_capacity_metrics(task_list)

    return SchedulerMetrics(
        productivity=productivity,
        reliability=reliability,
        capacity=capacity,
    )


def _inferred_note(prod: ProductivityMetrics) -> str:
    if prod.issues_attempted_inferred == 0:
        return ""
    return f" ({prod.issues_attempted_inferred} inferred from legacy state)"


def format_run_report(metrics: SchedulerMetrics) -> str:
    prod = metrics.productivity
    rel = metrics.reliability

    lines = [
        "========================================",
        "          SCHEDULER RUN REPORT          ",
        "========================================",
        "",
        "--- Productivity Metrics ---",
        f"Issues Attempted: {prod.issues_attempted}{_inferred_note(prod)}",
        f"Issues Implemented: {prod.issues_implemented}",
        f"Issues Ready For Review: {prod.issues_ready_for_review}",
        f"PRs Created: {prod.prs_created}",
    ]
    if prod.autonomous_completion_rate is not None:
        lines.append(
            f"Autonomous Issue Completion Rate: {prod.autonomous_completion_rate * 100:.1f}%"
        )
    else:
        lines.append("Autonomous Issue Completion Rate: N/A")

    lines.extend(
        [
            "",
            "--- Reliability Metrics ---",
        ]
    )
    if rel.task_completion_rate is not None:
        lines.append(f"Task Completion Rate: {rel.task_completion_rate * 100:.1f}%")
    else:
        lines.append("Task Completion Rate: N/A")

    if rel.manual_intervention_rate is not None:
        lines.append(f"Manual Intervention Rate: {rel.manual_intervention_rate * 100:.1f}%")
    else:
        lines.append("Manual Intervention Rate: N/A")

    lines.extend(
        [
            "",
            "--- Capacity Metrics ---",
            format_capacity_report(metrics.capacity),
            "========================================",
        ]
    )
    return "\n".join(lines)


def format_run_report_markdown(metrics: SchedulerMetrics) -> str:
    prod = metrics.productivity
    rel = metrics.reliability
    cap = metrics.capacity

    lines = [
        "# Scheduler Run Report",
        "",
        "## Productivity Metrics",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Issues Attempted | {prod.issues_attempted}{_inferred_note(prod)} |",
        f"| Issues Implemented | {prod.issues_implemented} |",
        f"| Issues Ready For Review | {prod.issues_ready_for_review} |",
        f"| PRs Created | {prod.prs_created} |",
    ]
    if prod.autonomous_completion_rate is not None:
        lines.append(
            f"| Autonomous Issue Completion Rate | {prod.autonomous_completion_rate * 100:.1f}% |"
        )
    else:
        lines.append("| Autonomous Issue Completion Rate | N/A |")

    lines.extend(
        [
            "",
            "## Reliability Metrics",
            "",
            "| Metric | Value |",
            "| --- | --- |",
        ]
    )
    if rel.task_completion_rate is not None:
        lines.append(f"| Task Completion Rate | {rel.task_completion_rate * 100:.1f}% |")
    else:
        lines.append("| Task Completion Rate | N/A |")

    if rel.manual_intervention_rate is not None:
        lines.append(f"| Manual Intervention Rate | {rel.manual_intervention_rate * 100:.1f}% |")
    else:
        lines.append("| Manual Intervention Rate | N/A |")

    lines.extend(
        [
            "",
            "## Capacity Metrics",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| Capacity Exhaustion Events | {cap.capacity_exhaustion_events} |",
            f"| Failover Attempts | {cap.failover_attempts} |",
            f"| Successful Continuations | {cap.successful_continuations} |",
            f"| Excluded Events | {cap.excluded_events} |",
        ]
    )
    if cap.failover_success_rate is not None:
        lines.append(f"| Capacity Failover Success Rate | {cap.failover_success_rate * 100:.1f}% |")
    else:
        lines.append("| Capacity Failover Success Rate | N/A |")

    if cap.exclusion_reasons:
        lines.extend(
            [
                "",
                "### Exclusion Reasons",
                "",
            ]
        )
        for r in cap.exclusion_reasons:
            lines.append(f"- {r}")

    return "\n".join(lines)
