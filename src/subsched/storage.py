from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import socket
import subprocess
import tempfile
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from subsched.models import Capacity, Task

SCHEMA_VERSION = 1
MAX_STATE_BYTES = 10 * 1024 * 1024

logger = logging.getLogger(__name__)


class StateCorruptionError(RuntimeError):
    pass


class SchedulerLockError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LockRecord:
    pid: int
    process_start_time: str
    nonce: str
    created_at: str
    hostname: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "process_start_time": self.process_start_time,
            "nonce": self.nonce,
            "created_at": self.created_at,
            "hostname": self.hostname,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LockRecord:
        return cls(
            pid=int(data["pid"]),
            process_start_time=str(data["process_start_time"]),
            nonce=str(data["nonce"]),
            created_at=str(data["created_at"]),
            hostname=str(data["hostname"]),
        )


def get_process_start_time(pid: int) -> str | None:
    if pid <= 0:
        return None
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def is_process_alive(pid: int, expected_start_time: str | None = None) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass

    if expected_start_time is not None:
        actual_start = get_process_start_time(pid)
        if actual_start is not None and actual_start != expected_start_time:
            return False

    return True


class SchedulerLock:
    # Per-lock-path thread locks, so instances guarding unrelated lock files (different
    # repositories/state directories) never block each other within the same process.
    # `_registry_guard` only protects registry lookups/inserts, not lock hold duration.
    _registry_guard: ClassVar[threading.Lock] = threading.Lock()
    _path_locks: ClassVar[dict[Path, threading.RLock]] = {}

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self.nonce: str | None = None
        self._held = False
        self._thread_lock = self._thread_lock_for(lock_path)

    @classmethod
    def _thread_lock_for(cls, lock_path: Path) -> threading.RLock:
        key = lock_path.resolve()
        with cls._registry_guard:
            thread_lock = cls._path_locks.get(key)
            if thread_lock is None:
                thread_lock = threading.RLock()
                cls._path_locks[key] = thread_lock
            return thread_lock

    def acquire(self) -> None:
        if self._held:
            return
        self._thread_lock.acquire()
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.lock_path.parent, 0o700)

            my_pid = os.getpid()
            my_start_time = get_process_start_time(my_pid) or datetime.now(UTC).isoformat()
            my_nonce = secrets.token_hex(16)
            my_record = LockRecord(
                pid=my_pid,
                process_start_time=my_start_time,
                nonce=my_nonce,
                created_at=datetime.now(UTC).isoformat(),
                hostname=socket.gethostname(),
            )

            for _ in range(2):
                try:
                    fd = os.open(
                        self.lock_path,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o600,
                    )
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            json.dump(my_record.to_dict(), f, indent=2)
                            f.write("\n")
                            f.flush()
                            os.fsync(f.fileno())
                    except BaseException:
                        with contextlib.suppress(OSError):
                            self.lock_path.unlink(missing_ok=True)
                        raise
                    self.nonce = my_nonce
                    self._held = True
                    return
                except FileExistsError as err:
                    existing = self._read_lock()
                    if existing is None:
                        with contextlib.suppress(OSError):
                            self.lock_path.unlink(missing_ok=True)
                        continue
                    if is_process_alive(existing.pid, existing.process_start_time):
                        raise SchedulerLockError(
                            f"scheduler is already running with PID {existing.pid} "
                            f"on {existing.hostname}"
                        ) from err
                    with contextlib.suppress(OSError):
                        self.lock_path.unlink(missing_ok=True)
                    continue

            raise SchedulerLockError("failed to acquire scheduler lock")
        except Exception:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        if not self._held:
            return
        try:
            existing = self._read_lock()
            if existing is not None and existing.nonce == self.nonce:
                self.lock_path.unlink(missing_ok=True)
        except OSError as err:
            logger.warning("failed to remove lock file %s: %s", self.lock_path, err)
        finally:
            self._held = False
            self.nonce = None
            self._thread_lock.release()

    def _read_lock(self) -> LockRecord | None:
        try:
            if not self.lock_path.exists():
                return None
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
            return LockRecord.from_dict(data)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def __enter__(self) -> SchedulerLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


class JsonStateStore:
    def __init__(self, repository: Path) -> None:
        self.state_dir = repository / ".ai"
        self.tasks_dir = self.state_dir / "tasks"
        self.handoffs_dir = self.state_dir / "handoffs"
        self.runtime_dir = self.state_dir / "runtime"
        self.quarantine_dir = self.state_dir / "quarantine"
        self.backup_dir = self.state_dir / "backup"
        self.path = self.state_dir / "scheduler.json"
        self.lock_file = self.state_dir / "scheduler.lock"

    def init_directories(self) -> None:
        self._validate_state_directory()
        for directory in (
            self.state_dir,
            self.tasks_dir,
            self.handoffs_dir,
            self.runtime_dir,
            self.quarantine_dir,
            self.backup_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)

    def lock(self) -> SchedulerLock:
        return SchedulerLock(self.lock_file)

    def get_revision(self) -> int:
        if not self.path.exists():
            return 0
        payload = self._load_payload()
        return int(payload.get("revision", 1))

    def save_tasks(
        self,
        tasks: Iterable[Task],
        *,
        paused: bool = False,
        expected_revision: int | None = None,
    ) -> None:
        capacities = self.load_capacities() if self.path.exists() else ()
        self.save_state(
            tasks,
            paused=paused,
            capacities=capacities,
            expected_revision=expected_revision,
        )

    def save_state(
        self,
        tasks: Iterable[Task],
        *,
        paused: bool = False,
        capacities: Iterable[Capacity] = (),
        expected_revision: int | None = None,
    ) -> None:
        current_revision = self.get_revision()
        if expected_revision is not None and current_revision != expected_revision:
            raise StateCorruptionError(
                f"lost update detected: expected revision {expected_revision}, "
                f"got {current_revision}"
            )
        new_revision = current_revision + 1

        payload = {
            "schema_version": SCHEMA_VERSION,
            "revision": new_revision,
            "paused": paused,
            "tasks": [task.to_dict() for task in tasks],
            "capacities": [capacity.to_dict() for capacity in capacities],
        }
        self.init_directories()

        if self.path.exists():
            backup_file = self.backup_dir / "scheduler.bak.json"
            try:
                backup_file.write_bytes(self.path.read_bytes())
            except OSError as err:
                logger.warning("failed to create backup before saving state: %s", err)

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
            self._quarantine_corrupt_file("invalid task data")
            raise StateCorruptionError("invalid task data in scheduler state") from error
        if len({task.issue_number for task in tasks}) != len(tasks):
            self._quarantine_corrupt_file("duplicate task")
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
            self._quarantine_corrupt_file("invalid capacity data")
            raise StateCorruptionError("invalid capacity data in scheduler state") from error

    def is_paused(self) -> bool:
        return bool(self._load_payload().get("paused", False)) if self.path.exists() else False

    def set_paused(self, paused: bool) -> None:
        with self.lock():
            self.save_tasks(self.load_tasks(), paused=paused)

    def _quarantine_corrupt_file(self, reason: str) -> Path | None:
        if not self.path.exists():
            return None
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        dest = self.quarantine_dir / f"scheduler-{timestamp}.corrupt.json"
        try:
            os.replace(self.path, dest)
            return dest
        except OSError:
            return None

    def _load_payload(self) -> dict[str, Any]:
        self._validate_state_directory()
        try:
            if self.path.stat().st_size > MAX_STATE_BYTES:
                self._quarantine_corrupt_file("oversized state file")
                raise StateCorruptionError("scheduler state exceeds the size limit")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            self._quarantine_corrupt_file("json decode error")
            raise StateCorruptionError(f"cannot parse scheduler state: {self.path}") from error
        except OSError as error:
            raise StateCorruptionError(f"cannot read scheduler state: {self.path}") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            self._quarantine_corrupt_file("schema version mismatch")
            raise StateCorruptionError("unsupported or missing scheduler state schema")
        if not isinstance(payload.get("paused", False), bool):
            self._quarantine_corrupt_file("invalid paused flag")
            raise StateCorruptionError("scheduler paused state must be boolean")
        if "revision" in payload:
            raw_rev = payload["revision"]
            if not isinstance(raw_rev, int) or isinstance(raw_rev, bool) or raw_rev < 0:
                self._quarantine_corrupt_file("invalid revision value")
                raise StateCorruptionError(
                    "scheduler state revision must be a non-negative integer"
                )
        return payload

    def _validate_state_directory(self) -> None:
        repository = self.state_dir.parent
        if repository.is_symlink():
            raise StateCorruptionError("repository path must not be a symlink")
        if self.state_dir.is_symlink():
            raise StateCorruptionError("scheduler state directory must not be a symlink")
        if self.path.is_symlink():
            raise StateCorruptionError("scheduler state file must not be a symlink")
