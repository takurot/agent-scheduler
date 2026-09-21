"""Operator-run Docker isolation check must fail closed before touching Docker."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live-isolation-check.sh"


def test_missing_images_fail_before_docker_is_called(tmp_path: Path) -> None:
    fake_docker = tmp_path / "docker"
    calls = tmp_path / "docker-called"
    fake_docker.write_text(f"#!/bin/sh\ntouch '{calls}'\nexit 0\n", encoding="utf-8")
    fake_docker.chmod(0o755)
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "SUBSCHED_ISOLATION_REPORT": str(tmp_path / "report.json"),
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=10
    )

    assert result.returncode != 0
    assert "SUBSCHED_ISOLATION_WORKER_IMAGE" in result.stderr
    assert not calls.exists()
    assert not (tmp_path / "report.json").exists()
