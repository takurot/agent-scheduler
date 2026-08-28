from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from subsched.agents.native import NativeWorker
from subsched.contract import bootstrap_task_files
from subsched.models import AgentResultKind, Issue, Task


def _install_fake_claude_on_path(bin_dir: Path) -> None:
    """A real, executable script named `claude` resolvable only via PATH -- exercising the
    actual subprocess.Popen(argv, env=...) + PATH-resolution path, not a mock."""
    script = bin_dir / "claude"
    payload = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 1,
            "result": "done",
        }
    )
    script.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write({payload!r})\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def test_native_worker_actually_launches_claude_via_path_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for #120 at the real subprocess boundary: no mocking of
    run_process_group/ClaudeAgent -- if NativeWorker builds env={} instead of a real
    environment, subprocess.Popen cannot resolve the bare command name "claude" via PATH
    and this fails with FileNotFoundError before the fake script ever runs."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _install_fake_claude_on_path(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    task = Task.from_issue(Issue(number=101, title="Test")).with_worktree(str(worktree))
    bootstrap_task_files(worktree, task)

    worker = NativeWorker(subscription_billing_verified=True)
    result = worker.run(task, "claude")

    assert result.kind is AgentResultKind.PASS, result.output
