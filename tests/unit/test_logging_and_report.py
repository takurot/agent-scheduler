from __future__ import annotations

import io
import json
from pathlib import Path

from subsched.capacity.metrics import CapacityMetrics
from subsched.logging import StructuredLogger, redact_sensitive_text
from subsched.metrics import (
    ProductivityMetrics,
    ReliabilityMetrics,
    SchedulerMetrics,
    calculate_metrics,
    format_run_report_markdown,
)
from subsched.models import Task, TaskState


def test_redact_sensitive_text() -> None:
    text = "Error with token ghp_ABC123XYZ456 and key sk-ant-api03-abcdefg and Bearer sec12345"
    redacted = redact_sensitive_text(text)
    assert "ghp_ABC123XYZ456" not in redacted
    assert "sk-ant-api03-abcdefg" not in redacted
    assert "Bearer [REDACTED]" in redacted or "[REDACTED]" in redacted


def test_structured_logger_jsonl(tmp_path: Path) -> None:
    log_file = tmp_path / "scheduler.jsonl"
    logger = StructuredLogger(log_file)

    logger.log(
        "task_dispatched",
        level="INFO",
        issue_number=101,
        agent="claude",
        message="Running task with token ghp_secret123",
        data={"env": "prod", "token": "ghp_secret456", "nested": ["sk-1234567890", 123]},
    )
    logger.log(
        "capacity_exhausted",
        level="WARN",
        issue_number=101,
        agent="claude",
        message="Claude session capacity exhausted",
    )

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["event"] == "task_dispatched"
    assert first["issue_number"] == 101
    assert first["agent"] == "claude"
    assert "ghp_secret123" not in first["message"]
    assert "ghp_secret456" not in json.dumps(first["data"])
    assert "sk-1234567890" not in json.dumps(first["data"])
    assert "[REDACTED]" in first["message"]

    second = json.loads(lines[1])
    assert second["event"] == "capacity_exhausted"
    assert second["level"] == "WARN"


def test_structured_logger_stream() -> None:
    stream = io.StringIO()
    logger = StructuredLogger(stream)
    logger.log("test_event", message="test message")
    output = stream.getvalue()
    assert "test_event" in output
    assert "test message" in output


def test_format_run_report_markdown() -> None:
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
    metrics = calculate_metrics([t1])
    md = format_run_report_markdown(metrics)

    assert "# Scheduler Run Report" in md
    assert "| Metric | Value |" in md
    assert "Autonomous Issue Completion Rate" in md
    assert "Capacity Failover Success Rate" in md
    assert "100.0%" in md

    # Empty metrics
    empty_metrics = SchedulerMetrics(
        productivity=ProductivityMetrics(0, 0, 0, None),
        reliability=ReliabilityMetrics(None, None, None),
        capacity=CapacityMetrics(0, 0, 0, 0, None, exclusion_reasons=("Reason 1",)),
    )
    empty_md = format_run_report_markdown(empty_metrics)
    assert "N/A" in empty_md
    assert "Exclusion Reasons" in empty_md
    assert "- Reason 1" in empty_md
