from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from subsched.cli import app
from subsched.metrics import (
    ProductivityMetrics,
    ReliabilityMetrics,
    SchedulerMetrics,
    calculate_metrics,
    format_run_report,
)
from subsched.models import Task, TaskState
from subsched.storage import JsonStateStore

_DISPATCHED_AT = datetime(2026, 9, 1, tzinfo=UTC)


def _task(issue: int, status: TaskState, **kwargs: object) -> Task:
    return Task(
        task_id=f"github-{issue}",
        issue_number=issue,
        title=f"Task {issue}",
        labels=(),
        status=status,
        **kwargs,  # type: ignore[arg-type]
    )


def test_calculate_metrics_comprehensive() -> None:
    t1 = Task(
        task_id="github-101",
        issue_number=101,
        title="Task 101",
        labels=(),
        status=TaskState.COMPLETE,
        capacity_events=1,
        agent_switches=1,
        pr=1,
        run_started_at=_DISPATCHED_AT,
    )
    t2 = Task(
        task_id="github-102",
        issue_number=102,
        title="Task 102",
        labels=(),
        status=TaskState.READY_FOR_REVIEW,
        capacity_events=0,
        pr=2,
        run_started_at=_DISPATCHED_AT,
    )
    t3 = Task(
        task_id="github-103",
        issue_number=103,
        title="Task 103",
        labels=(),
        status=TaskState.NEEDS_HUMAN,
        capacity_events=0,
        run_started_at=_DISPATCHED_AT,
    )
    t4 = Task(
        task_id="github-104",
        issue_number=104,
        title="Task 104",
        labels=(),
        status=TaskState.DISCOVERED,
    )

    metrics = calculate_metrics([t1, t2, t3, t4])

    # Productivity
    assert metrics.productivity.issues_attempted == 3  # t1, t2, t3
    assert metrics.productivity.issues_implemented == 2  # t1 (COMPLETE), t2 (READY_FOR_REVIEW)
    assert metrics.productivity.prs_created == 2
    assert metrics.productivity.autonomous_completion_rate == round(2 / 3, 4)
    # Regression test for #142: implemented, ready-for-review, and complete must be
    # separately observable -- previously PR creation jumped straight to COMPLETE, so
    # ready_for_review was always 0 and indistinguishable from completed.
    assert metrics.productivity.issues_ready_for_review == 1  # t2 only

    # Reliability
    assert metrics.reliability.task_completion_rate == round(1 / 3, 4)
    assert metrics.reliability.manual_intervention_rate == round(1 / 3, 4)

    # Capacity
    assert metrics.capacity.capacity_exhaustion_events == 1
    assert metrics.capacity.failover_success_rate == 1.0


def test_format_run_report() -> None:
    metrics = SchedulerMetrics(
        productivity=ProductivityMetrics(
            issues_attempted=10,
            issues_implemented=9,
            prs_created=9,
            autonomous_completion_rate=0.9,
        ),
        reliability=ReliabilityMetrics(
            task_completion_rate=0.8,
            manual_intervention_rate=0.1,
            agent_failure_switch_rate=0.0,
        ),
        capacity=calculate_metrics([]).capacity,
    )

    report = format_run_report(metrics)
    assert "Autonomous Issue Completion Rate: 90.0%" in report
    assert "Task Completion Rate: 80.0%" in report
    assert "Manual Intervention Rate: 10.0%" in report


def test_cli_metrics_command(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    t = Task(
        task_id="github-101",
        issue_number=101,
        title="Task 101",
        labels=(),
        status=TaskState.COMPLETE,
        pr=1,
    )
    store.save_tasks([t])

    runner = CliRunner()
    res = runner.invoke(app, ["--repository", str(tmp_path), "metrics"])
    assert res.exit_code == 0
    assert "Productivity Metrics" in res.stdout

    # JSON mode
    res_json = runner.invoke(app, ["--repository", str(tmp_path), "metrics", "--json"])
    assert res_json.exit_code == 0
    parsed = json.loads(res_json.stdout)
    assert "productivity" in parsed
    assert parsed["productivity"]["issues_attempted"] == 1

    # Report file output
    report_out = tmp_path / "run_report.txt"
    res_report = runner.invoke(
        app, ["--repository", str(tmp_path), "metrics", "--report", str(report_out)]
    )
    assert res_report.exit_code == 0
    assert report_out.is_file()
    assert "SCHEDULER RUN REPORT" in report_out.read_text(encoding="utf-8")


# --- #378: denominators must reflect dispatch history, not current status ----------


def test_undispatched_queue_reports_no_attempts_and_null_rates() -> None:
    """READY / WAITING_DEPENDENCY / BLOCKED tasks were never dispatched, so they must
    not inflate issues_attempted or drag the completion rates down to 0.0."""
    metrics = calculate_metrics(
        [
            _task(1, TaskState.READY),
            _task(2, TaskState.WAITING_DEPENDENCY, dependencies=(1,)),
            _task(3, TaskState.BLOCKED),
        ]
    )

    assert metrics.productivity.issues_attempted == 0
    assert metrics.productivity.autonomous_completion_rate is None
    assert metrics.reliability.task_completion_rate is None
    assert metrics.reliability.manual_intervention_rate is None
    assert "Autonomous Issue Completion Rate: N/A" in format_run_report(metrics)


def test_adding_waiting_tasks_does_not_change_rates() -> None:
    done = _task(1, TaskState.COMPLETE, pr=5, run_started_at=_DISPATCHED_AT)
    baseline = calculate_metrics([done])
    with_waiting = calculate_metrics(
        [done, _task(2, TaskState.READY), _task(3, TaskState.WAITING_DEPENDENCY)]
    )

    assert with_waiting.productivity == baseline.productivity
    assert with_waiting.reliability == baseline.reliability


def test_mixed_fixture_counts_only_dispatched_tasks() -> None:
    tasks = [
        _task(1, TaskState.COMPLETE, pr=1, run_started_at=_DISPATCHED_AT),
        _task(2, TaskState.NEEDS_HUMAN, run_started_at=_DISPATCHED_AT),
        # capacity failover: dispatched, then waiting for the next attempt
        _task(3, TaskState.WAITING_CAPACITY, capacity_events=1, run_started_at=_DISPATCHED_AT),
        # cancelled before it ever ran
        _task(4, TaskState.CANCELLED),
        # never dispatched
        _task(5, TaskState.READY),
        # escalated without ever being dispatched (e.g. eligibility rejection)
        _task(6, TaskState.NEEDS_HUMAN),
    ]

    metrics = calculate_metrics(tasks)

    assert metrics.productivity.issues_attempted == 3
    assert metrics.productivity.issues_implemented == 1
    assert metrics.productivity.autonomous_completion_rate == round(1 / 3, 4)
    assert metrics.reliability.task_completion_rate == round(1 / 3, 4)
    assert metrics.reliability.manual_intervention_rate == round(1 / 3, 4)
    assert metrics.productivity.issues_attempted_inferred == 0


def test_legacy_state_without_run_started_at_is_inferred_and_flagged() -> None:
    """Pre-#137 state has no run_started_at; dispatch is inferred from other durable
    evidence and reported separately from confirmed attempts."""
    tasks = [
        _task(1, TaskState.READY_FOR_REVIEW, pr=3),  # PR exists => it was dispatched
        _task(2, TaskState.NEEDS_HUMAN, attempt=1),  # attempt counter recorded a dispatch
        _task(3, TaskState.READY),  # no evidence
        _task(4, TaskState.COMPLETE, pr=4, run_started_at=_DISPATCHED_AT),  # confirmed
    ]

    metrics = calculate_metrics(tasks)

    assert metrics.productivity.issues_attempted == 3
    assert metrics.productivity.issues_attempted_inferred == 2
    assert "inferred" in format_run_report(metrics).lower()


def test_failure_switch_rate_excludes_capacity_switches() -> None:
    """Capacity-driven switches must not be counted as failure switches (SPEC: capacity
    events are not Agent failures)."""
    capacity_only = _task(
        1,
        TaskState.COMPLETE,
        pr=1,
        run_started_at=_DISPATCHED_AT,
        capacity_events=2,
        agent_switches=2,
        actual_agent_switches=2,
    )
    failure_switch = _task(
        2,
        TaskState.READY_FOR_REVIEW,
        pr=2,
        run_started_at=_DISPATCHED_AT,
        per_agent_failures=(("claude", 2),),
        actual_agent_switches=1,
    )

    assert calculate_metrics([capacity_only]).reliability.agent_failure_switch_rate is None
    rate = calculate_metrics([capacity_only, failure_switch]).reliability.agent_failure_switch_rate
    assert rate == round(1 / 2, 4)
