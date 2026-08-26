from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

from subsched.capacity.metrics import CapacityMetrics
from subsched.metrics import (
    ProductivityMetrics,
    ReliabilityMetrics,
    SchedulerMetrics,
    calculate_metrics,
    format_run_report_markdown,
)
from subsched.models import Capacity, CapacityState, Issue, Task, TaskState
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler
from subsched.storage import JsonStateStore
from subsched.structured_logger import StructuredLogger, redact_sensitive_text


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


class _RaisingWorker:
    """Worker double that raises instead of returning an AgentResult, simulating an
    unexpected exception inside worker.run() (regression coverage for #104)."""

    def run(self, task: Task, agent: str) -> object:
        raise RuntimeError("connection reset by peer, token=ghp_secret123abc")


def _available(agent: str) -> Capacity:
    return Capacity(
        agent=agent,
        state=CapacityState.AVAILABLE,
        observed_at=datetime.now(UTC),
        source="provider",
        confidence="high",
    )


def test_worker_exception_logs_detail_without_leaking_it_into_agent_result(
    tmp_path: Path,
) -> None:
    """Regression test for #104: a worker exception's type name still becomes
    AgentResult.output (unchanged, so it can't leak secrets into task state), but the
    full (redacted) exception detail must now reach the structured logger for
    troubleshooting, correlated by issue number and agent."""
    stream = io.StringIO()
    logger = StructuredLogger(stream)

    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router([AgentConfig("claude", priority=100)]),
        worker=_RaisingWorker(),
        worktree_root=tmp_path / "worktrees",
        structured_logger=logger,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude")])

    task = scheduler.tasks[0]
    # AgentResult.output stays minimal -- just the exception type name, no message.
    assert task.per_agent_failures == (("claude", 1),)

    lines = [json.loads(line) for line in stream.getvalue().strip().splitlines()]
    exception_entries = [entry for entry in lines if entry["event"] == "worker_exception"]
    assert len(exception_entries) == 1
    entry = exception_entries[0]
    assert entry["issue_number"] == 101
    assert entry["agent"] == "claude"
    assert entry["level"] == "ERROR"
    assert "RuntimeError" in json.dumps(entry)
    assert "connection reset by peer" in entry["message"]
    # The structured logger's own redaction must still apply to the exception message.
    assert "ghp_secret123abc" not in json.dumps(entry)
    assert "[REDACTED]" in entry["message"]


def test_worker_exception_without_structured_logger_still_completes(tmp_path: Path) -> None:
    """The structured_logger must be optional -- existing callers that don't configure
    one must keep working exactly as before (no crash, same AgentResult.output)."""
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router([AgentConfig("claude", priority=100)]),
        worker=_RaisingWorker(),
        worktree_root=tmp_path / "worktrees",
    )
    scheduler.discover([Issue(number=101, title="Task 101")])
    scheduler.tick([_available("claude")])

    task = scheduler.tasks[0]
    assert task.per_agent_failures == (("claude", 1),)
