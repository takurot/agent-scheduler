from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

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

    def __init__(self, target: Path | TextIO) -> None:
        self.target = target

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
            self.target.parent.mkdir(parents=True, exist_ok=True)
            with self.target.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        else:
            self.target.write(line)
            self.target.flush()

        return entry
