from __future__ import annotations

import json
from pathlib import Path

from subsched.metrics import calculate_metrics
from subsched.models import Task, TaskState
from subsched.notifications import (
    NotificationEvent,
    NotificationOutbox,
    NotificationSinkError,
    write_run_summary,
)


def _task(issue: int, status: TaskState) -> Task:
    return Task(
        task_id=f"github-{issue}",
        issue_number=issue,
        title=f"Task {issue}",
        labels=(),
        status=status,
    )


def test_write_run_summary_creates_markdown_and_json(tmp_path: Path) -> None:
    metrics = calculate_metrics([_task(1, TaskState.PR_READY), _task(2, TaskState.NEEDS_HUMAN)])

    md_path, json_path = write_run_summary(
        tmp_path, run_id="abc123", metrics=metrics, needs_human_issues=[2]
    )

    assert md_path.read_text(encoding="utf-8").startswith("# Scheduler Run Report")
    assert "## Needs Human" in md_path.read_text(encoding="utf-8")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["run_id"] == "abc123"
    assert payload["needs_human_issues"] == [2]
    assert "productivity" in payload


def test_write_run_summary_redacts_secrets_in_exclusion_reasons(tmp_path: Path) -> None:
    metrics = calculate_metrics([_task(1, TaskState.PR_READY)])
    object.__setattr__(
        metrics.capacity, "exclusion_reasons", ("token ghp_abcdefghijklmnopqrstuvwxyz012345",)
    )

    md_path, _ = write_run_summary(tmp_path, run_id="r1", metrics=metrics, needs_human_issues=[])

    assert "ghp_" not in md_path.read_text(encoding="utf-8")
    assert "[REDACTED]" in md_path.read_text(encoding="utf-8")


def test_outbox_dedup_skips_duplicate_run_event(tmp_path: Path) -> None:
    outbox = NotificationOutbox(tmp_path / "outbox.json")
    event = NotificationEvent(run_id="r1", event_type="run_complete", issue_number=None)

    first = outbox.enqueue(event)
    second = outbox.enqueue(event)

    assert first is True
    assert second is False


def test_outbox_dedup_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    event = NotificationEvent(run_id="r1", event_type="needs_human", issue_number=5)
    NotificationOutbox(path).enqueue(event)

    reloaded = NotificationOutbox(path)
    assert reloaded.enqueue(event) is False


def test_outbox_delivery_success_marks_delivered(tmp_path: Path) -> None:
    outbox = NotificationOutbox(tmp_path / "outbox.json")
    event = NotificationEvent(run_id="r1", event_type="run_complete", issue_number=None)
    outbox.enqueue(event)

    delivered: list[str] = []
    outbox.deliver_pending(lambda evt: delivered.append(evt.key), max_attempts=3)

    assert delivered == [event.key]
    assert outbox.status(event.key) == "delivered"


def test_outbox_delivery_failure_retries_then_marks_dead(tmp_path: Path) -> None:
    outbox = NotificationOutbox(tmp_path / "outbox.json")
    event = NotificationEvent(run_id="r1", event_type="run_complete", issue_number=None)
    outbox.enqueue(event)

    def failing_sink(evt: NotificationEvent) -> None:
        raise NotificationSinkError("destination unreachable")

    outbox.deliver_pending(failing_sink, max_attempts=2)
    assert outbox.status(event.key) == "pending"
    outbox.deliver_pending(failing_sink, max_attempts=2)
    assert outbox.status(event.key) == "dead"


def test_outbox_delivery_failure_does_not_raise(tmp_path: Path) -> None:
    outbox = NotificationOutbox(tmp_path / "outbox.json")
    event = NotificationEvent(run_id="r1", event_type="run_complete", issue_number=None)
    outbox.enqueue(event)

    def failing_sink(evt: NotificationEvent) -> None:
        raise NotificationSinkError("boom")

    # must not raise -- destination failure never bubbles up to task/queue state
    outbox.deliver_pending(failing_sink, max_attempts=5)
