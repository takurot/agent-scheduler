from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from subsched.agents.claude import cleanup_claude_process_group


def test_fake_cli_timeout_kills_and_reaps_process_group() -> None:
    script = """
import os
import signal
import subprocess
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)",
    ]
)
print(child.pid, flush=True)
time.sleep(60)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline())

    assert cleanup_claude_process_group(process, grace_seconds=0.05) is True
    assert process.returncode == -9

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("fake CLI descendant remained alive after process-group cleanup")


def test_cleanup_kills_detached_descendant_after_leader_exits_on_sigterm(
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "heartbeat"
    script = """
import subprocess
import sys
import time

child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import pathlib,signal,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); "
        "path=pathlib.Path(sys.argv[1]); "
        "[(path.write_text(str(i)), time.sleep(0.01)) for i in range(6000)]",
        sys.argv[1],
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
)
assert child.stdout is not None
assert child.stdout.readline().strip() == "ready"
child.stdout.close()
print(child.pid, flush=True)
time.sleep(60)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(heartbeat)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert int(process.stdout.readline()) > 0

    assert cleanup_claude_process_group(process, grace_seconds=0.2) is True
    assert process.returncode == -15

    heartbeat_after_cleanup = heartbeat.read_text(encoding="utf-8")
    time.sleep(0.05)
    assert heartbeat.read_text(encoding="utf-8") == heartbeat_after_cleanup
