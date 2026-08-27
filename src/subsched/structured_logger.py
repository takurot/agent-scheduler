from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

# #141: cap on the JSONL file's size before it is rotated to a single ".1" backup. A
# long-running scheduler emits one line per lifecycle event per run indefinitely
# otherwise, which is exactly the unbounded-growth gap this issue exists to close.
# Single-generation rotation (not numbered/timestamped history) keeps this simple: the
# log is for recent-run observability, not a permanent audit trail (state/checkpoint/PR
# already carry the durable record -- see #143).
DEFAULT_MAX_BYTES = 10 * 1024 * 1024

_SENSITIVE_PATTERNS = [
    re.compile(r"ghp_[a-zA-Z0-9_]+", re.IGNORECASE),
    re.compile(r"gho_[a-zA-Z0-9_]+", re.IGNORECASE),
    re.compile(r"github_pat_[a-zA-Z0-9_]+", re.IGNORECASE),
    re.compile(r"sk-[a-zA-Z0-9_-]+", re.IGNORECASE),
    re.compile(r"Bearer\s+[a-zA-Z0-9_.-]+", re.IGNORECASE),
    re.compile(r"(api[_-]?key\s*[:=]\s*['\"]?)[a-zA-Z0-9_.-]+(['\"]?)", re.IGNORECASE),
    re.compile(r"(password\s*[:=]\s*['\"]?)[a-zA-Z0-9_.-]+(['\"]?)", re.IGNORECASE),
]


def redact_sensitive_text(text: str) -> str:
    """Redact tokens, credentials, and api keys from text."""
    redacted = text
    for pat in _SENSITIVE_PATTERNS:
        redacted = pat.sub("[REDACTED]", redacted)
    return redacted


def _redact_data(data: Any) -> Any:
    if isinstance(data, str):
        return redact_sensitive_text(data)
    elif isinstance(data, dict):
        return {k: _redact_data(v) for k, v in data.items()}
    elif isinstance(data, (list, tuple)):
        return [_redact_data(item) for item in data]
    return data


class StructuredLogger:
    """JSON Lines structured logger for scheduler events with automated redaction."""

    def __init__(self, target: Path | TextIO, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.target = target
        self.max_bytes = max_bytes

    def _rotate_if_oversized(self, incoming_bytes: int) -> None:
        """Rotate target -> target.1 (overwriting any existing .1) when appending the
        next line would push the file past max_bytes. Only called when self.target is a
        Path and already exists. Best-effort: an OSError here (e.g. concurrent rotation
        from another process) is swallowed so log rotation never blocks the scheduler's
        actual work -- the file simply keeps growing past max_bytes for that one write,
        which is the safe direction to fail in.
        """
        assert isinstance(self.target, Path)
        try:
            current_size = self.target.stat().st_size
        except OSError:
            return
        if current_size + incoming_bytes <= self.max_bytes:
            return
        rotated = self.target.with_name(self.target.name + ".1")
        try:
            os.replace(self.target, rotated)
            os.chmod(rotated, 0o600)
        except OSError:
            pass

    def log(
        self,
        event: str,
        *,
        level: str = "INFO",
        issue_number: int | None = None,
        agent: str | None = None,
        task_id: str | None = None,
        message: str = "",
        data: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> dict[str, Any]:
        ts = (timestamp or datetime.now(UTC)).isoformat()
        entry: dict[str, Any] = {
            "timestamp": ts,
            "level": level.upper(),
            "event": event,
        }
        if issue_number is not None:
            entry["issue_number"] = issue_number
        if agent is not None:
            entry["agent"] = agent
        if task_id is not None:
            entry["task_id"] = task_id
        if message:
            entry["message"] = redact_sensitive_text(message)
        if data is not None:
            entry["data"] = _redact_data(data)

        line = json.dumps(entry, ensure_ascii=False) + "\n"

        if isinstance(self.target, Path):
            # #141: this JSONL log can contain issue numbers, agent names, and task
            # detail -- like other runtime state (#143's backup/checkpoint hardening),
            # it must not be left world-/group-readable under the process umask, and a
            # file created by a pre-hardening subsched version must be repaired in place
            # (chmod only, content untouched) rather than staying exposed forever.
            self.target.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.target.parent, 0o700)
            if self.target.exists() and not self.target.is_symlink():
                os.chmod(self.target, 0o600)
                self._rotate_if_oversized(len(line.encode("utf-8")))
            descriptor = os.open(self.target, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        else:
            self.target.write(line)
            self.target.flush()

        return entry
