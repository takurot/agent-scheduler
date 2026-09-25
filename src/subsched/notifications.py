from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from subsched.metrics import SchedulerMetrics, format_run_report_markdown
from subsched.storage import atomic_write_secure_bytes, secure_directory
from subsched.structured_logger import redact_sensitive_text

EventType = Literal["run_complete", "needs_human"]

_STATUS_PENDING = "pending"
_STATUS_DELIVERED = "delivered"
_STATUS_DEAD = "dead"
_RECORD_FIELDS = {
    "status",
    "attempts",
    "run_id",
    "event_type",
    "issue_number",
    "created_at",
}
_VALID_STATUSES = {_STATUS_PENDING, _STATUS_DELIVERED, _STATUS_DEAD}


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

    def __post_init__(self) -> None:
        if (
            not isinstance(self.run_id, str)
            or not self.run_id
            or len(self.run_id) > 128
            or any(character.isspace() or ord(character) < 32 for character in self.run_id)
            or "/" in self.run_id
            or "\\" in self.run_id
        ):
            raise ValueError("run_id must be a non-empty safe identifier")
        if self.event_type not in ("run_complete", "needs_human"):
            raise ValueError("event_type is invalid")
        valid_issue = (
            isinstance(self.issue_number, int)
            and not isinstance(self.issue_number, bool)
            and self.issue_number > 0
        )
        if self.event_type == "needs_human" and not valid_issue:
            raise ValueError("needs_human issue_number must be a positive integer")
        if self.event_type == "run_complete" and self.issue_number is not None:
            raise ValueError("run_complete issue_number must be null")
        if not isinstance(self.created_at, str):
            raise ValueError("created_at must be an ISO 8601 timestamp")
        try:
            parsed_created_at = datetime.fromisoformat(self.created_at)
        except ValueError as error:
            raise ValueError("created_at must be an ISO 8601 timestamp") from error
        if parsed_created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")

    @property
    def key(self) -> str:
        return f"{self.run_id}:{self.event_type}:{self.issue_number}"


class NotificationSinkError(Exception):
    """Raised by a notification sink when delivery to its destination fails."""


NotificationSink = Callable[[NotificationEvent], None]


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    secure_directory(path.parent)
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _validated_records(path: Path, raw: object) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ValueError(f"corrupt notification outbox: {path}")
    validated: dict[str, dict[str, Any]] = {}
    try:
        for key, record in raw.items():
            if not isinstance(key, str) or not isinstance(record, dict):
                raise ValueError
            if set(record) != _RECORD_FIELDS:
                raise ValueError
            status = record["status"]
            attempts = record["attempts"]
            if status not in _VALID_STATUSES:
                raise ValueError
            if (
                not isinstance(attempts, int)
                or isinstance(attempts, bool)
                or attempts < 0
            ):
                raise ValueError
            event = NotificationEvent(
                run_id=record["run_id"],
                event_type=cast(EventType, record["event_type"]),
                issue_number=record["issue_number"],
                created_at=record["created_at"],
            )
            if key != event.key:
                raise ValueError
            validated[key] = dict(record)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"corrupt notification outbox: {path}") from error
    return validated


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
        self._lock_path = path.with_name(f"{path.name}.lock")
        with _exclusive_file_lock(self._lock_path):
            self._records = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        if self._path.is_symlink():
            raise OSError(f"refusing to read symlinked outbox: {self._path}")
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            raise ValueError(f"corrupt notification outbox: {self._path}") from error
        return _validated_records(self._path, raw)

    def _save(self) -> None:
        secure_directory(self._path.parent)
        data = json.dumps(self._records, indent=2, ensure_ascii=False)
        atomic_write_secure_bytes(self._path, data.encode("utf-8"))

    def enqueue(self, event: NotificationEvent) -> bool:
        """Return True if newly enqueued, False if this key was already recorded
        (pending, delivered, or dead) -- the dedup guarantee."""
        with _exclusive_file_lock(self._lock_path):
            self._records = self._load()
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
        with _exclusive_file_lock(self._lock_path):
            self._records = self._load()
            record = self._records.get(key)
            return None if record is None else str(record["status"])

    def deliver_pending(self, sink: NotificationSink, *, max_attempts: int) -> None:
        """Attempt delivery of every pending event via `sink`. A failing sink marks
        the event for retry (bounded by `max_attempts`) and is swallowed -- this
        method never raises, so a broken destination cannot affect task/queue state."""
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        with _exclusive_file_lock(self._lock_path):
            self._records = self._load()
            for record in self._records.values():
                if record["status"] != _STATUS_PENDING:
                    continue
                if record["attempts"] >= max_attempts:
                    record["status"] = _STATUS_DEAD
                    try:
                        self._save()
                    except OSError:
                        return
                    continue
                event = NotificationEvent(
                    run_id=record["run_id"],
                    event_type=record["event_type"],
                    issue_number=record["issue_number"],
                    created_at=record["created_at"],
                )
                record["attempts"] += 1
                try:
                    self._save()
                except OSError:
                    return
                try:
                    sink(event)
                except Exception:
                    if record["attempts"] >= max_attempts:
                        record["status"] = _STATUS_DEAD
                else:
                    record["status"] = _STATUS_DELIVERED
                try:
                    self._save()
                except OSError:
                    return


def local_file_sink(path: Path) -> NotificationSink:
    """Default notification sink for this stage: append delivered events to a local
    JSONL file. No external webhook/endpoint is contacted here -- opt-in remote
    delivery is out of scope for this stage (see issue #386)."""

    def _deliver(event: NotificationEvent) -> None:
        secure_directory(path.parent)
        delivery_key = hashlib.sha256(event.key.encode("utf-8")).hexdigest()
        lock_path = path.with_name(f"{path.name}.lock")
        with _exclusive_file_lock(lock_path):
            if path.exists():
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, encoding="utf-8") as handle:
                    for existing_line in handle:
                        try:
                            existing = json.loads(existing_line)
                            existing_key = existing.get("delivery_key")
                            if existing_key is None:
                                legacy_key = (
                                    f'{existing["run_id"]}:{existing["event_type"]}:'
                                    f'{existing["issue_number"]}'
                                )
                                existing_key = hashlib.sha256(
                                    legacy_key.encode("utf-8")
                                ).hexdigest()
                        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as error:
                            raise NotificationSinkError(
                                f"corrupt local notification sink: {path}"
                            ) from error
                        if existing_key == delivery_key:
                            return
            line = redact_sensitive_text(
                json.dumps(
                    {
                        "delivery_key": delivery_key,
                        "run_id": event.run_id,
                        "event_type": event.event_type,
                        "issue_number": event.issue_number,
                        "created_at": event.created_at,
                    },
                    ensure_ascii=False,
                )
            )
            descriptor = os.open(
                path,
                os.O_CREAT
                | os.O_APPEND
                | os.O_WRONLY
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    return _deliver
