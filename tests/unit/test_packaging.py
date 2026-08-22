from __future__ import annotations

import subprocess
import sys

from subsched.cli import app


def test_python_module_entrypoint() -> None:
    res = subprocess.run(
        [sys.executable, "-m", "subsched", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert "Subscription-aware coding agent scheduler" in res.stdout


def test_cli_app_commands_registered() -> None:
    command_names = {
        c.name or (c.callback.__name__ if c.callback else None)
        for c in app.registered_commands
    }
    expected = {"run", "status", "pause", "resume", "cancel", "doctor", "metrics"}
    assert expected.issubset(command_names)
