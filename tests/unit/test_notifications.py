from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import subsched.notifications as notifications_module
from subsched.metrics import calculate_metrics
from subsched.models import Task, TaskState
from subsched.notifications import (
    NotificationEvent,
    NotificationOutbox,
    NotificationSinkError,
    local_file_sink,
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


def test_local_delivery_is_idempotent_after_outbox_save_failure_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outbox_path = tmp_path / "outbox.json"
    delivered_path = tmp_path / "delivered.jsonl"
    event = NotificationEvent(run_id="r1", event_type="run_complete")
    outbox = NotificationOutbox(outbox_path)
    outbox.enqueue(event)
    sink = local_file_sink(delivered_path)
    sink_completed = False
    real_atomic_write = notifications_module.atomic_write_secure_bytes

    def tracked_sink(delivered_event: NotificationEvent) -> None:
        nonlocal sink_completed
        sink(delivered_event)
        sink_completed = True

    def fail_after_sink(path: Path, data: bytes, *, mode: int = 0o600) -> None:
        if sink_completed:
            raise OSError("simulated outbox persistence failure")
        real_atomic_write(path, data, mode=mode)

    monkeypatch.setattr(notifications_module, "atomic_write_secure_bytes", fail_after_sink)
    outbox.deliver_pending(tracked_sink, max_attempts=3)
    monkeypatch.setattr(notifications_module, "atomic_write_secure_bytes", real_atomic_write)

    reloaded = NotificationOutbox(outbox_path)
    reloaded.deliver_pending(local_file_sink(delivered_path), max_attempts=3)

    lines = delivered_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert reloaded.status(event.key) == "delivered"


def test_retry_attempt_is_durable_before_sink_side_effect_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outbox_path = tmp_path / "outbox.json"
    event = NotificationEvent(run_id="r1", event_type="run_complete")
    outbox = NotificationOutbox(outbox_path)
    outbox.enqueue(event)
    sink_called = False
    real_atomic_write = notifications_module.atomic_write_secure_bytes

    def failing_sink(_: NotificationEvent) -> None:
        nonlocal sink_called
        sink_called = True
        raise NotificationSinkError("destination unavailable")

    def fail_after_sink(path: Path, data: bytes, *, mode: int = 0o600) -> None:
        if sink_called:
            raise OSError("simulated status persistence failure")
        real_atomic_write(path, data, mode=mode)

    monkeypatch.setattr(notifications_module, "atomic_write_secure_bytes", fail_after_sink)
    outbox.deliver_pending(failing_sink, max_attempts=2)
    monkeypatch.setattr(notifications_module, "atomic_write_secure_bytes", real_atomic_write)

    reloaded = NotificationOutbox(outbox_path)
    reloaded.deliver_pending(failing_sink, max_attempts=2)

    assert reloaded.status(event.key) == "dead"


def test_stale_outbox_instances_do_not_duplicate_delivery(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    event = NotificationEvent(run_id="r1", event_type="run_complete")
    first = NotificationOutbox(path)
    first.enqueue(event)
    second = NotificationOutbox(path)
    delivered: list[str] = []

    first.deliver_pending(lambda item: delivered.append(item.key), max_attempts=3)
    second.deliver_pending(lambda item: delivered.append(item.key), max_attempts=3)

    assert delivered == [event.key]


def test_concurrent_outbox_instances_serialize_delivery(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    event = NotificationEvent(run_id="r1", event_type="run_complete")
    NotificationOutbox(path).enqueue(event)
    first = NotificationOutbox(path)
    second = NotificationOutbox(path)
    first_sink_started = threading.Event()
    release_first_sink = threading.Event()
    delivered: list[str] = []

    def blocking_sink(item: NotificationEvent) -> None:
        delivered.append(item.key)
        first_sink_started.set()
        release_first_sink.wait(timeout=2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(first.deliver_pending, blocking_sink, max_attempts=3)
        assert first_sink_started.wait(timeout=2)
        second_result = executor.submit(second.deliver_pending, blocking_sink, max_attempts=3)
        release_first_sink.set()
        first_result.result(timeout=2)
        second_result.result(timeout=2)

    assert delivered == [event.key]


def test_stale_outbox_instances_preserve_each_others_enqueues(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    first = NotificationOutbox(path)
    second = NotificationOutbox(path)
    run_event = NotificationEvent(run_id="r1", event_type="run_complete")
    human_event = NotificationEvent(run_id="r1", event_type="needs_human", issue_number=7)

    first.enqueue(run_event)
    second.enqueue(human_event)

    reloaded = NotificationOutbox(path)
    assert reloaded.status(run_event.key) == "pending"
    assert reloaded.status(human_event.key) == "pending"


def _valid_record() -> tuple[str, dict[str, Any]]:
    event = NotificationEvent(run_id="r1", event_type="needs_human", issue_number=7)
    return event.key, {
        "status": "pending",
        "attempts": 0,
        "run_id": event.run_id,
        "event_type": event.event_type,
        "issue_number": event.issue_number,
        "created_at": event.created_at,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_type", "unknown"),
        ("issue_number", "7"),
        ("attempts", -1),
        ("created_at", "not-a-date"),
        ("status", "unknown"),
    ],
)
def test_outbox_rejects_malformed_persisted_record_without_overwriting(
    tmp_path: Path, field: str, value: object
) -> None:
    path = tmp_path / "outbox.json"
    key, record = _valid_record()
    record[field] = value
    original = json.dumps({key: record})
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="corrupt notification outbox"):
        NotificationOutbox(path)

    assert path.read_text(encoding="utf-8") == original


def test_outbox_rejects_mismatched_persisted_dedup_key(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    _, record = _valid_record()
    path.write_text(json.dumps({"wrong:key": record}), encoding="utf-8")

    with pytest.raises(ValueError, match="corrupt notification outbox"):
        NotificationOutbox(path)


def test_notification_event_requires_issue_only_for_needs_human() -> None:
    with pytest.raises(ValueError, match="issue_number"):
        NotificationEvent(run_id="r1", event_type="needs_human")
    with pytest.raises(ValueError, match="issue_number"):
        NotificationEvent(run_id="r1", event_type="run_complete", issue_number=7)


def test_local_file_sink_redacts_sensitive_metadata(tmp_path: Path) -> None:
    path = tmp_path / "delivered.jsonl"
    sink = local_file_sink(path)

    sink(
        NotificationEvent(
            run_id="ghp_abcdefghijklmnopqrstuvwxyz012345",
            event_type="run_complete",
        )
    )

    payload = path.read_text(encoding="utf-8")
    assert "ghp_" not in payload
    assert "[REDACTED]" in payload


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is unavailable")
def test_local_file_sink_refuses_symlink_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    path = tmp_path / "delivered.jsonl"
    path.symlink_to(outside)

    sink = local_file_sink(path)

    with pytest.raises(OSError):
        sink(NotificationEvent(run_id="r1", event_type="run_complete"))
    assert not outside.exists()
