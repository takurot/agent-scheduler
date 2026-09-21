"""Durable lifecycle records for detached MCP dispatches (#383)."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from subsched.storage import atomic_write_secure_bytes, get_process_start_time, secure_directory

_RUN_ID = re.compile(r"[0-9a-f]{24}\Z")
_STATUSES = frozenset({"accepted", "starting", "running", "succeeded", "failed"})
_MAX_RECORD_BYTES = 4096
_MAX_LOG_BYTES = 64 * 1024
_MAX_DIAGNOSTIC_BYTES = 16 * 1024


def _failure_reason(output: bytes) -> str:
    lowered = output.decode("utf-8", errors="replace").casefold()
    if any(
        item in lowered
        for item in ("config error", "configuration error", "invalid subsched.yaml")
    ):
        return "config_error"
    if "pre-flight" in lowered or "preflight" in lowered:
        return "preflight_failed"
    if "scheduler is already running" in lowered or "scheduler lock" in lowered:
        return "scheduler_locked"
    return "cli_exit_nonzero"


class DispatchRunStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.directory = runtime_dir / "dispatch-runs"

    def _path(self, run_id: str, suffix: str = ".json") -> Path:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("invalid run_id")
        if self.directory.parent.is_symlink():
            raise ValueError("runtime directory is a symlink")
        if self.directory.is_symlink():
            raise ValueError("dispatch run directory is a symlink")
        path = self.directory / f"{run_id}{suffix}"
        if path.is_symlink():
            raise ValueError("dispatch run file is a symlink")
        return path

    def create(self) -> str:
        secure_directory(self.directory)
        for _ in range(3):
            run_id = secrets.token_hex(12)
            path = self._path(run_id)
            if path.exists():
                continue
            now = datetime.now(UTC).isoformat()
            record = {
                "run_id": run_id,
                "status": "accepted",
                "created_at": now,
                "updated_at": now,
                "pid": None,
                "process_start_time": None,
                "exit_code": None,
                "reason": None,
            }
            atomic_write_secure_bytes(path, json.dumps(record, sort_keys=True).encode())
            self._log(run_id, "accepted")
            return run_id
        raise OSError("could not allocate a unique dispatch run")

    def _read(self, run_id: str) -> dict[str, Any]:
        path = self._path(run_id)
        with path.open("rb") as handle:
            raw = handle.read(_MAX_RECORD_BYTES + 1)
        if len(raw) > _MAX_RECORD_BYTES:
            raise ValueError("dispatch run record is too large")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid dispatch run record") from error
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "run_id", "status", "created_at", "updated_at", "pid",
                "process_start_time", "exit_code", "reason",
            }
            or value["run_id"] != run_id
            or value["status"] not in _STATUSES
            or not isinstance(value["created_at"], str)
            or not isinstance(value["updated_at"], str)
            or (value["pid"] is not None and (type(value["pid"]) is not int or value["pid"] <= 0))
            or (
                value["process_start_time"] is not None
                and not isinstance(value["process_start_time"], str)
            )
            or (value["exit_code"] is not None and type(value["exit_code"]) is not int)
            or (value["reason"] is not None and value["reason"] not in {
                "spawn_failed", "identity_unavailable", "cli_exit_nonzero",
                "config_error", "preflight_failed", "scheduler_locked",
            })
        ):
            raise ValueError("invalid dispatch run record")
        return value

    def _log(self, run_id: str, event: str) -> None:
        # The private log contains lifecycle codes only, never provider stdout/stderr.
        path = self._path(run_id, ".log")
        if path.exists() and path.stat().st_size >= _MAX_LOG_BYTES:
            backup = self._path(run_id, ".log.1")
            os.replace(path, backup)
        if path.exists():
            os.chmod(path, 0o600)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "ab") as handle:
            handle.write(f"{datetime.now(UTC).isoformat()} {event}\n".encode())
            handle.flush()
            os.fsync(handle.fileno())

    def update(
        self,
        run_id: str,
        *,
        status: str,
        pid: int | None = None,
        process_start_time: str | None = None,
        exit_code: int | None = None,
        reason: str | None = None,
    ) -> None:
        if status not in _STATUSES:
            raise ValueError("invalid dispatch run status")
        value = self._read(run_id)
        value.update(
            status=status,
            updated_at=datetime.now(UTC).isoformat(),
            pid=pid,
            process_start_time=process_start_time,
            exit_code=exit_code,
            reason=reason,
        )
        atomic_write_secure_bytes(self._path(run_id), json.dumps(value, sort_keys=True).encode())
        self._log(run_id, f"{status} reason={reason or 'none'} exit={exit_code}")

    def inspect(self, run_id: str) -> dict[str, Any]:
        value = self._read(run_id)
        if value["status"] == "accepted":
            try:
                created = datetime.fromisoformat(value["created_at"])
            except ValueError as error:
                raise ValueError("invalid dispatch run timestamp") from error
            if datetime.now(UTC) - created > timedelta(seconds=10):
                value["status"] = "stale"
        elif value["status"] in {"starting", "running"}:
            pid = value["pid"]
            start = value["process_start_time"]
            if pid is None or start is None or get_process_start_time(pid) != start:
                value["status"] = "stale"
        return value


def run_child(store: DispatchRunStore, run_id: str, argv: list[str]) -> int:
    """Execute a detached CLI run while recording only sanitized lifecycle codes."""
    pid = os.getpid()
    start = get_process_start_time(pid)
    if start is None:
        store.update(run_id, status="failed", reason="identity_unavailable")
        return 1
    store.update(run_id, status="starting", pid=pid, process_start_time=start)
    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.SubprocessError):
        store.update(
            run_id, status="failed", pid=pid, process_start_time=start, reason="spawn_failed"
        )
        return 1
    store.update(run_id, status="running", pid=pid, process_start_time=start)
    assert child.stdout is not None
    diagnostic = bytearray()
    while chunk := child.stdout.read(4096):
        remaining = _MAX_DIAGNOSTIC_BYTES - len(diagnostic)
        if remaining > 0:
            diagnostic.extend(chunk[:remaining])
    exit_code = child.wait()
    store.update(
        run_id,
        status="succeeded" if exit_code == 0 else "failed",
        pid=pid,
        process_start_time=start,
        exit_code=exit_code,
        reason=None if exit_code == 0 else _failure_reason(bytes(diagnostic)),
    )
    return exit_code


if __name__ == "__main__":
    repository = Path(sys.argv[1])
    dispatch_id = sys.argv[2]
    command = sys.argv[3:]
    sys.exit(run_child(DispatchRunStore(repository / ".ai" / "runtime"), dispatch_id, command))
