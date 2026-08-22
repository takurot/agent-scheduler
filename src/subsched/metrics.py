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


def calculate_metrics(tasks: Iterable[Task]) -> SchedulerMetrics:
    task_list = tuple(tasks)
    attempted = [
        t
        for t in task_list
        if t.status not in {TaskState.DISCOVERED, TaskState.ELIGIBILITY_CHECK}
    ]
    num_attempted = len(attempted)

    implemented = [
        t
        for t in attempted
        if t.status in {TaskState.READY_FOR_REVIEW, TaskState.COMPLETE, TaskState.PR_READY}
    ]
    num_implemented = len(implemented)

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
    total_switches = sum(t.actual_agent_switches for t in task_list)
    switch_rate = (
        round(total_switches / total_failures, 4) if total_failures > 0 else None
    )

    productivity = ProductivityMetrics(
        issues_attempted=num_attempted,
        issues_implemented=num_implemented,
        prs_created=num_prs,
        autonomous_completion_rate=auto_rate,
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


def format_run_report(metrics: SchedulerMetrics) -> str:
    prod = metrics.productivity
    rel = metrics.reliability

    lines = [
        "========================================",
        "          SCHEDULER RUN REPORT          ",
        "========================================",
        "",
        "--- Productivity Metrics ---",
        f"Issues Attempted: {prod.issues_attempted}",
        f"Issues Implemented: {prod.issues_implemented}",
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
        f"| Issues Attempted | {prod.issues_attempted} |",
        f"| Issues Implemented | {prod.issues_implemented} |",
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
