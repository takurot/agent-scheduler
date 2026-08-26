from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from subsched.agents.base import ProcessExecutionRequest
from subsched.agents.process import (
    _reap_proc,
    filter_environment,
    redact_sensitive_command_audit,
    run_process_group,
    stop_process_group,
)


def test_process_execution_request_validations(tmp_path: Path) -> None:
    # Empty argv
    with pytest.raises(ValueError, match="argv must not be empty"):
        ProcessExecutionRequest(argv=(), cwd=tmp_path, env={})

    # Relative cwd
    with pytest.raises(ValueError, match="cwd must be an absolute"):
        ProcessExecutionRequest(argv=("echo",), cwd=Path("relative"), env={})

    # Invalid timeout
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        ProcessExecutionRequest(argv=("echo",), cwd=tmp_path, env={}, timeout_seconds=0)

    # Invalid grace
    with pytest.raises(ValueError, match="grace_seconds must be positive"):
        ProcessExecutionRequest(argv=("echo",), cwd=tmp_path, env={}, grace_seconds=0)

    # Invalid output limit
    with pytest.raises(ValueError, match="output_limit_bytes must be positive"):
        ProcessExecutionRequest(argv=("echo",), cwd=tmp_path, env={}, output_limit_bytes=0)


def test_filter_environment_applies_allowlist() -> None:
    env = {
        "PATH": "/usr/bin",
        "HOME": "/Users/test",
        "SECRET_TOKEN": "super_secret_value",
        "GITHUB_TOKEN": "ghp_1234567890abcdef",
    }
    filtered = filter_environment(env, allowlist={"PATH", "HOME"})
    assert filtered == {"PATH": "/usr/bin", "HOME": "/Users/test"}
    assert "SECRET_TOKEN" not in filtered
    assert "GITHUB_TOKEN" not in filtered


def test_redact_sensitive_command_audit() -> None:
    argv = (
        "git",
        "clone",
        "https://ghp_abcdef1234567890@github.com/org/repo.git",
        "--token",
        "github_pat_11AAAAAAA0000000000000_1234567890abcdef",
    )
    redacted = redact_sensitive_command_audit(argv)
    assert "ghp_abcdef1234567890" not in " ".join(redacted)
    assert "github_pat_11AAAAAAA" not in " ".join(redacted)
    assert any("[REDACTED]" in arg for arg in redacted)


def test_run_process_group_success(tmp_path: Path) -> None:
    request = ProcessExecutionRequest(
        argv=(sys.executable, "-c", "print('hello world')"),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        timeout_seconds=5.0,
        output_limit_bytes=1024,
    )
    result = run_process_group(request)
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello world"
    assert result.stderr == ""
    assert result.timed_out is False
    assert result.output_limit_exceeded is False
    assert result.cleanup_succeeded is True
    assert result.command_not_found is False


def test_run_process_group_command_not_found(tmp_path: Path) -> None:
    """Regression test for #129: launching a nonexistent command (e.g. a bare `pytest`
    that isn't on PATH in a uv-managed project) must be distinguishable from a command
    that actually ran and exited non-zero, not just collapsed into a generic exit_code=1
    result that looks identical to a real gate failure."""
    request = ProcessExecutionRequest(
        argv=("subsched-definitely-does-not-exist-abcxyz",),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        timeout_seconds=5.0,
    )
    result = run_process_group(request)
    assert result.command_not_found is True
    assert result.exit_code != 0
    assert "subsched-definitely-does-not-exist-abcxyz" in result.stderr


def test_run_process_group_output_size_limit(tmp_path: Path) -> None:
    request = ProcessExecutionRequest(
        argv=(sys.executable, "-c", "import sys; sys.stdout.write('A' * 2000)"),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        timeout_seconds=5.0,
        output_limit_bytes=500,
    )
    result = run_process_group(request)
    assert result.output_limit_exceeded is True
    assert len(result.stdout) <= 500
    assert result.cleanup_succeeded is True


def test_run_process_group_timeout_terminates_group(tmp_path: Path) -> None:
    request = ProcessExecutionRequest(
        argv=(
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(10)",
        ),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        timeout_seconds=0.2,
        grace_seconds=0.2,
    )
    result = run_process_group(request)
    assert result.timed_out is True
    assert result.cleanup_succeeded is True


def test_run_process_group_detects_and_cleans_zombies_and_child_processes(tmp_path: Path) -> None:
    child_marker = tmp_path / "child_running.txt"
    script = f"""
import subprocess
import sys
import time

child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "path = pathlib.Path('{child_marker}'); "
        "[(path.write_text(str(i)), time.sleep(0.02)) for i in range(1000)]"
    ]
)
time.sleep(10)
"""
    request = ProcessExecutionRequest(
        argv=(sys.executable, "-c", script),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        timeout_seconds=0.3,
        grace_seconds=0.2,
    )
    result = run_process_group(request)
    assert result.timed_out is True
    assert result.cleanup_succeeded is True

    if child_marker.exists():
        val1 = child_marker.read_text(encoding="utf-8")
        import time

        time.sleep(0.1)
        val2 = child_marker.read_text(encoding="utf-8")
        assert val1 == val2


def test_run_process_group_cleans_up_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the wait for the subprocess is interrupted (e.g. Ctrl+C in the foreground CLI
    loop), the child process group must still be terminated instead of left running."""
    import subprocess as subprocess_module
    import time

    marker = tmp_path / "still_running.txt"
    script = (
        "import pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"path = pathlib.Path({str(marker)!r}); "
        "[(path.write_text(str(i)), time.sleep(0.02)) for i in range(1000)]"
    )
    request = ProcessExecutionRequest(
        argv=(sys.executable, "-c", script),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        timeout_seconds=5.0,
        grace_seconds=0.2,
    )

    original_wait = subprocess_module.Popen.wait
    triggered = False

    def wait_then_interrupt(
        self: subprocess_module.Popen[bytes], timeout: float | None = None
    ) -> int:
        # Only the *first* wait() call (run_process_group's own) should simulate Ctrl+C.
        # stop_process_group()'s cleanup waits (via _reap_proc) must run for real, or this
        # test can't distinguish "cleanup ran" from "cleanup itself got interrupted".
        nonlocal triggered
        if triggered:
            return original_wait(self, timeout=timeout)
        triggered = True
        # Let the child actually start and begin writing before we simulate Ctrl+C, so the
        # assertion below observes a genuinely running process, not a race against startup.
        deadline = time.monotonic() + 2.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess_module.Popen, "wait", wait_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        run_process_group(request)

    assert marker.exists(), "child process never started writing before the interrupt"
    val1 = marker.read_text(encoding="utf-8")
    time.sleep(0.1)
    val2 = marker.read_text(encoding="utf-8")
    assert val1 == val2, "child process group kept running after KeyboardInterrupt"


def test_stop_process_group_edge_cases() -> None:
    assert stop_process_group(-1, 0.1) is False
    assert _reap_proc(None, 0.1) is True


def test_streams_properly_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import queue

    from subsched.agents.process import _read_bounded_stream, _write_stream

    class TrackingBytesIO(io.BytesIO):
        def __init__(self, initial_bytes: bytes = b"") -> None:
            super().__init__(initial_bytes)
            self.closed_count = 0

        def close(self) -> None:
            self.closed_count += 1
            super().close()

    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)

    # Read stream close
    read_stream = TrackingBytesIO(b"sample data")
    q: queue.Queue[tuple[bytes, bool, bool]] = queue.Queue()
    _read_bounded_stream(read_stream, 100, 99999, q)
    assert read_stream.closed_count >= 1

    # Write stream close
    write_stream = TrackingBytesIO()
    _write_stream(write_stream, b"payload")
    assert write_stream.closed_count >= 1
