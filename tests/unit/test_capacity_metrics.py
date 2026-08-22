from __future__ import annotations

from subsched.capacity.metrics import (
    CapacityMetrics,
    calculate_capacity_metrics,
    format_capacity_report,
)
from subsched.models import Task, TaskState


def test_capacity_metrics_no_events() -> None:
    t1 = Task(
        task_id="github-1",
        issue_number=1,
        title="Task 1",
        labels=(),
        status=TaskState.COMPLETE,
        capacity_events=0,
    )
    metrics = calculate_capacity_metrics([t1])
    assert metrics.capacity_exhaustion_events == 0
    assert metrics.failover_attempts == 0
    assert metrics.successful_continuations == 0
    assert metrics.failover_success_rate is None

    report = format_capacity_report(metrics)
    assert "Failover Success Rate: N/A" in report


def test_capacity_metrics_successful_failover() -> None:
    t1 = Task(
        task_id="github-101",
        issue_number=101,
        title="Task 101",
        labels=(),
        status=TaskState.COMPLETE,
        capacity_events=1,
        agent_switches=1,
    )
    t2 = Task(
        task_id="github-102",
        issue_number=102,
        title="Task 102",
        labels=(),
        status=TaskState.READY_FOR_REVIEW,
        capacity_events=2,
        agent_switches=2,
    )
    metrics = calculate_capacity_metrics([t1, t2])
    assert metrics.capacity_exhaustion_events == 3
    assert metrics.failover_attempts == 3
    assert metrics.successful_continuations == 3
    assert metrics.failover_success_rate == 1.0
    d = metrics.to_dict()
    assert d["failover_success_rate"] == 1.0


def test_capacity_metrics_unsuccessful_failover() -> None:
    t_failed = Task(
        task_id="github-103",
        issue_number=103,
        title="Task 103",
        labels=(),
        status=TaskState.NEEDS_HUMAN,
        capacity_events=6,
        agent_switches=6,
    )
    t_term_failed = Task(
        task_id="github-106",
        issue_number=106,
        title="Task 106",
        labels=(),
        status=TaskState.FAILED,
        capacity_events=1,
        agent_switches=1,
    )
    metrics = calculate_capacity_metrics([t_failed, t_term_failed])
    assert metrics.capacity_exhaustion_events == 7
    assert metrics.failover_attempts == 7
    assert metrics.successful_continuations == 0
    assert metrics.failover_success_rate == 0.0


def test_capacity_metrics_with_exclusions() -> None:
    t_cancelled = Task(
        task_id="github-104",
        issue_number=104,
        title="Task 104",
        labels=(),
        status=TaskState.CANCELLED,
        capacity_events=1,
        agent_switches=1,
    )
    t_ok = Task(
        task_id="github-105",
        issue_number=105,
        title="Task 105",
        labels=(),
        status=TaskState.COMPLETE,
        capacity_events=1,
        agent_switches=1,
    )
    metrics = calculate_capacity_metrics([t_cancelled, t_ok])
    assert metrics.capacity_exhaustion_events == 2
    assert metrics.excluded_events == 1
    assert metrics.failover_attempts == 1
    assert metrics.successful_continuations == 1
    assert metrics.failover_success_rate == 1.0

    report = format_capacity_report(metrics)
    assert "Exclusion Reasons:" in report
    assert "Task #104" in report


def test_format_capacity_report() -> None:
    metrics = CapacityMetrics(
        capacity_exhaustion_events=4,
        failover_attempts=4,
        successful_continuations=4,
        excluded_events=0,
        failover_success_rate=1.0,
    )
    report = format_capacity_report(metrics)
    assert "Capacity Exhaustion Events: 4" in report
    assert "Failover Success Rate: 100.0%" in report
