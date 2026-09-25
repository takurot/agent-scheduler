from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from subsched.metrics import SchedulerMetrics, format_run_report_markdown
from subsched.storage import atomic_write_secure_bytes, secure_directory
from subsched.structured_logger import redact_sensitive_text

EventType = Literal["run_complete", "needs_human"]

_STATUS_PENDING = "pending"
_STATUS_DELIVERED = "delivered"
_STATUS_DEAD = "dead"


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """A single run-scoped notification (overnight run completion or NEEDS_HUMAN).

    `key` is the dedup identity: reprocessing the same run/event/issue combination
    must not enqueue or deliver it twice (see NotificationOutbox).
    """

    run_id: str
    event_type: EventType
    issue_number: int | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def key(self) -> str:
        return f"{self.run_id}:{self.event_type}:{self.issue_number}"


class NotificationSinkError(Exception):
    """Raised by a notification sink when delivery to its destination fails."""


NotificationSink = Callable[[NotificationEvent], None]


def write_run_summary(
    out_dir: Path,
    *,
    run_id: str,
    metrics: SchedulerMetrics,
    needs_human_issues: Sequence[int],
) -> tuple[Path, Path]:
    """Write a per-run Markdown + JSON summary (issue numbers only, no raw
    issue/provider text or credentials) so an operator can review overnight run
    completion and NEEDS_HUMAN tasks without re-reading the JSONL log."""
    secure_directory(out_dir)

    markdown = format_run_report_markdown(metrics)
    markdown = redact_sensitive_text(markdown)
    if needs_human_issues:
        needs_human_lines = "\n".join(f"- #{n}" for n in needs_human_issues)
        markdown += f"\n\n## Needs Human\n\n{needs_human_lines}\n"
    else:
        markdown += "\n\n## Needs Human\n\nNone\n"

    payload: dict[str, Any] = {
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "needs_human_issues": list(needs_human_issues),
        **metrics.to_dict(),
    }
    json_text = redact_sensitive_text(json.dumps(payload, indent=2, ensure_ascii=False))

    md_path = out_dir / f"run-{run_id}.md"
    json_path = out_dir / f"run-{run_id}.json"
    atomic_write_secure_bytes(md_path, markdown.encode("utf-8"))
    atomic_write_secure_bytes(json_path, (json_text + "\n").encode("utf-8"))
    return md_path, json_path


class NotificationOutbox:
    """Local, durable outbox of notification events keyed by `NotificationEvent.key`
    so the same run/event is never enqueued or delivered twice. Delivery failures
    are retried up to a bounded attempt count and never raise -- a broken or
    unconfigured destination must not affect task state or the scheduler queue
    (see docs/WORKFLOW.md fail-closed invariant, applied here as fail-silent for
    the notification side-channel specifically)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._records: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        if self._path.is_symlink():
            raise OSError(f"refusing to read symlinked outbox: {self._path}")
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"corrupt notification outbox: {self._path}")
        return raw

    def _save(self) -> None:
        secure_directory(self._path.parent)
        data = json.dumps(self._records, indent=2, ensure_ascii=False)
        atomic_write_secure_bytes(self._path, data.encode("utf-8"))

    def enqueue(self, event: NotificationEvent) -> bool:
        """Return True if newly enqueued, False if this key was already recorded
        (pending, delivered, or dead) -- the dedup guarantee."""
        if event.key in self._records:
            return False
        self._records[event.key] = {
            "status": _STATUS_PENDING,
            "attempts": 0,
            "run_id": event.run_id,
            "event_type": event.event_type,
            "issue_number": event.issue_number,
            "created_at": event.created_at,
        }
        self._save()
        return True

    def status(self, key: str) -> str | None:
        record = self._records.get(key)
        return None if record is None else str(record["status"])

    def deliver_pending(self, sink: NotificationSink, *, max_attempts: int) -> None:
        """Attempt delivery of every pending event via `sink`. A failing sink marks
        the event for retry (bounded by `max_attempts`) and is swallowed -- this
        method never raises, so a broken destination cannot affect task/queue state."""
        changed = False
        for record in self._records.values():
            if record["status"] != _STATUS_PENDING:
                continue
            event = NotificationEvent(
                run_id=record["run_id"],
                event_type=record["event_type"],
                issue_number=record["issue_number"],
                created_at=record["created_at"],
            )
            try:
                sink(event)
            except Exception:
                record["attempts"] += 1
                if record["attempts"] >= max_attempts:
                    record["status"] = _STATUS_DEAD
            else:
                record["status"] = _STATUS_DELIVERED
            changed = True
        if changed:
            self._save()


def local_file_sink(path: Path) -> NotificationSink:
    """Default notification sink for this stage: append delivered events to a local
    JSONL file. No external webhook/endpoint is contacted here -- opt-in remote
    delivery is out of scope for this stage (see issue #386)."""

    def _deliver(event: NotificationEvent) -> None:
        secure_directory(path.parent)
        line = json.dumps(
            {
                "run_id": event.run_id,
                "event_type": event.event_type,
                "issue_number": event.issue_number,
                "created_at": event.created_at,
            },
            ensure_ascii=False,
        )
        descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    return _deliver
