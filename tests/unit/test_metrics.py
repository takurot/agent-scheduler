from __future__ import annotations

import json
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
    )
    t2 = Task(
        task_id="github-102",
        issue_number=102,
        title="Task 102",
        labels=(),
        status=TaskState.READY_FOR_REVIEW,
        capacity_events=0,
        pr=2,
    )
    t3 = Task(
        task_id="github-103",
        issue_number=103,
        title="Task 103",
        labels=(),
        status=TaskState.NEEDS_HUMAN,
        capacity_events=0,
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
