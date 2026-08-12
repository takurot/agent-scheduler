from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from subsched.models import Capacity, Task

SCHEMA_VERSION = 1
MAX_STATE_BYTES = 10 * 1024 * 1024


class StateCorruptionError(RuntimeError):
    pass


class JsonStateStore:
    def __init__(self, repository: Path) -> None:
        self.state_dir = repository / ".ai"
        self.path = self.state_dir / "scheduler.json"

    def save_tasks(self, tasks: Iterable[Task], *, paused: bool = False) -> None:
        capacities = self.load_capacities() if self.path.exists() else ()
        self.save_state(tasks, paused=paused, capacities=capacities)

    def save_state(
        self,
        tasks: Iterable[Task],
        *,
        paused: bool = False,
        capacities: Iterable[Capacity] = (),
    ) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "paused": paused,
            "tasks": [task.to_dict() for task in tasks],
            "capacities": [capacity.to_dict() for capacity in capacities],
        }
        self._validate_state_directory()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="scheduler.", suffix=".tmp", dir=self.state_dir
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
            directory_descriptor = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    def load_tasks(self) -> tuple[Task, ...]:
        if not self.path.exists():
            return ()
        payload = self._load_payload()
        try:
            tasks = tuple(Task.from_dict(item) for item in payload["tasks"])
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise StateCorruptionError("invalid task data in scheduler state") from error
        if len({task.issue_number for task in tasks}) != len(tasks):
            raise StateCorruptionError("duplicate task in scheduler state")
        return tasks

    def load_capacities(self) -> tuple[Capacity, ...]:
        if not self.path.exists():
            return ()
        payload = self._load_payload()
        try:
            raw = payload.get("capacities", [])
            if not isinstance(raw, list):
                raise TypeError
            return tuple(Capacity.from_dict(item) for item in raw)
        except (AttributeError, TypeError, ValueError) as error:
            raise StateCorruptionError("invalid capacity data in scheduler state") from error

    def is_paused(self) -> bool:
        return bool(self._load_payload().get("paused", False)) if self.path.exists() else False

    def set_paused(self, paused: bool) -> None:
        self.save_tasks(self.load_tasks(), paused=paused)

    def _load_payload(self) -> dict[str, Any]:
        self._validate_state_directory()
        try:
            if self.path.stat().st_size > MAX_STATE_BYTES:
                raise StateCorruptionError("scheduler state exceeds the size limit")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateCorruptionError(f"cannot read scheduler state: {self.path}") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise StateCorruptionError("unsupported or missing scheduler state schema")
        if not isinstance(payload.get("paused", False), bool):
            raise StateCorruptionError("scheduler paused state must be boolean")
        return payload

    def _validate_state_directory(self) -> None:
        repository = self.state_dir.parent
        if repository.is_symlink():
            raise StateCorruptionError("repository path must not be a symlink")
        if self.state_dir.is_symlink():
            raise StateCorruptionError("scheduler state directory must not be a symlink")
        if self.path.is_symlink():
            raise StateCorruptionError("scheduler state file must not be a symlink")
