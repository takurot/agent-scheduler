from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import threading
import time
from contextlib import suppress
from typing import Any

from subsched.agents.base import ProcessExecutionRequest, ProcessExecutionResult

COMMON_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PATH",
        "TERM",
        "USER",
        "TMPDIR",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
    }
)

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{10,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
)


def filter_environment(
    env: dict[str, str], allowlist: frozenset[str] | set[str] = COMMON_ENV_ALLOWLIST
) -> dict[str, str]:
    """Filter environment variables using a strict allowlist."""
    return {name: value for name, value in env.items() if name in allowlist}


def redact_sensitive_command_audit(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Redact secrets and sensitive tokens from argv for safe logging and auditing."""
    redacted: list[str] = []
    for arg in argv:
        clean_arg = arg
        for pattern in SECRET_PATTERNS:
            clean_arg = pattern.sub("[REDACTED]", clean_arg)
        redacted.append(clean_arg)
    return tuple(redacted)


def run_process_group(request: ProcessExecutionRequest) -> ProcessExecutionResult:
    """Run a subprocess in an isolated process group with timeouts and stream limits."""
    try:
        process = subprocess.Popen(
            list(request.argv),
            cwd=str(request.cwd),
            env=request.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as e:
        return ProcessExecutionResult(
            exit_code=1,
            stdout="",
            stderr=str(e),
            timed_out=False,
            output_limit_exceeded=False,
            cleanup_succeeded=True,
            command_not_found=isinstance(e, FileNotFoundError),
        )

    output_queue: queue.Queue[tuple[bytes, bool, bool]] = queue.Queue(maxsize=1)
    stderr_queue: queue.Queue[tuple[bytes, bool, bool]] = queue.Queue(maxsize=1)

    stdout_reader = threading.Thread(
        target=_read_bounded_stream,
        args=(process.stdout, request.output_limit_bytes, process.pid, output_queue),
        daemon=True,
    )
    stderr_reader = threading.Thread(
        target=_read_bounded_stream,
        args=(process.stderr, request.output_limit_bytes, process.pid, stderr_queue),
        daemon=True,
    )
    stdin_writer = threading.Thread(
        target=_write_stream,
        args=(process.stdin, request.stdin_payload),
        daemon=True,
    )

    stdout_reader.start()
    stderr_reader.start()
    stdin_writer.start()

    try:
        # #141: poll in heartbeat_interval_seconds increments instead of a single
        # process.wait(timeout=full_timeout) call, so a heartbeat callback can observe
        # progress on a long-running agent invocation without busy-polling (the loop only
        # wakes up once per heartbeat_interval_seconds, default 60s) and without changing
        # the overall timeout/cleanup semantics below.
        deadline = time.monotonic() + request.timeout_seconds
        elapsed = 0.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd=request.argv, timeout=request.timeout_seconds)
            wait_for = min(remaining, request.heartbeat_interval_seconds)
            try:
                process.wait(timeout=wait_for)
                break
            except subprocess.TimeoutExpired:
                elapsed += wait_for
                if request.heartbeat is not None:
                    request.heartbeat(elapsed)
                continue
    except subprocess.TimeoutExpired:
        cleanup_ok = stop_process_group(process, request.grace_seconds)
        return ProcessExecutionResult(
            exit_code=-signal.SIGKILL if not cleanup_ok else process.returncode or -signal.SIGTERM,
            stdout="",
            stderr="timed out",
            timed_out=True,
            output_limit_exceeded=False,
            cleanup_succeeded=cleanup_ok,
        )
    except BaseException:
        # Covers KeyboardInterrupt (Ctrl+C) as well as ordinary exceptions raised while
        # waiting: the child process group must not be left running unattended.
        stop_process_group(process, request.grace_seconds)
        raise

    stdout_reader.join(timeout=request.grace_seconds)
    stderr_reader.join(timeout=request.grace_seconds)
    stdin_writer.join(timeout=request.grace_seconds)

    try:
        stdout_bytes, stdout_overflow, _ = output_queue.get_nowait()
    except queue.Empty:
        stdout_bytes, stdout_overflow = b"", False

    try:
        stderr_bytes, stderr_overflow, _ = stderr_queue.get_nowait()
    except queue.Empty:
        stderr_bytes, stderr_overflow = b"", False

    overflow = stdout_overflow or stderr_overflow
    if overflow:
        cleanup_ok = stop_process_group(process, request.grace_seconds)
        return ProcessExecutionResult(
            exit_code=process.returncode or 1,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr="output limit exceeded",
            timed_out=False,
            output_limit_exceeded=True,
            cleanup_succeeded=cleanup_ok,
        )

    cleanup_ok = stop_process_group(process, request.grace_seconds)
    return ProcessExecutionResult(
        exit_code=process.returncode,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        timed_out=False,
        output_limit_exceeded=False,
        cleanup_succeeded=cleanup_ok,
    )


def stop_process_group(
    process: subprocess.Popen[bytes] | subprocess.Popen[str] | int,
    grace_seconds: float,
) -> bool:
    """Send SIGTERM to process group, wait for grace period, then SIGKILL if alive."""
    pid = process.pid if hasattr(process, "pid") else process
    proc_obj = process if hasattr(process, "wait") else None

    if pid <= 0:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return _reap_proc(proc_obj, grace_seconds)
    except OSError:
        return False

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        _reap_proc(proc_obj, timeout=0.001)
        if not _process_group_alive(pid):
            break
        time.sleep(min(0.01, grace_seconds))

    if _process_group_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        _reap_proc(proc_obj, timeout=0.001)
        if not _process_group_alive(pid):
            break
        time.sleep(min(0.01, grace_seconds))

    _reap_proc(proc_obj, grace_seconds)
    return not _process_group_alive(pid)


def _reap_proc(proc: Any, timeout: float) -> bool:
    if proc is None:
        return True
    try:
        proc.wait(timeout=timeout)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _process_group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _read_bounded_stream(
    stream: Any,
    limit_bytes: int,
    pgid: int,
    out_queue: queue.Queue[tuple[bytes, bool, bool]],
) -> None:
    if stream is None:
        out_queue.put((b"", False, True))
        return
    buf = b""
    try:
        while chunk := stream.read(min(65536, limit_bytes + 1 - len(buf))):
            buf += chunk
            if len(buf) > limit_bytes:
                with suppress(OSError):
                    os.killpg(pgid, signal.SIGTERM)
                out_queue.put((buf[:limit_bytes], True, False))
                return
    except OSError:
        out_queue.put((buf, False, True))
        return
    finally:
        with suppress(OSError):
            stream.close()
    out_queue.put((buf, False, False))


def _write_stream(
    stream: Any,
    payload: bytes,
) -> None:
    if stream is None:
        return
    try:
        if payload:
            stream.write(payload)
    except (BrokenPipeError, OSError):
        pass
    finally:
        with suppress(OSError):
            stream.close()
