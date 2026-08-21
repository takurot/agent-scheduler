from __future__ import annotations

import sys
from pathlib import Path

from subsched.verification import run_verification


def test_run_verification_all_pass(tmp_path: Path) -> None:
    commands = (
        f"{sys.executable} -c \"print('gate1 ok')\"",
        f"{sys.executable} -c \"print('gate2 ok')\"",
    )
    report = run_verification(tmp_path, commands)
    assert report.passed is True
    assert len(report.gates) == 2
    assert report.gates[0].passed is True
    assert report.gates[1].passed is True
    assert "PASS" in report.summary


def test_run_verification_failure_stops_pipeline(tmp_path: Path) -> None:
    commands = (
        f"{sys.executable} -c \"import sys; sys.exit(1)\"",
        f"{sys.executable} -c \"print('should not run')\"",
    )
    report = run_verification(tmp_path, commands)
    assert report.passed is False
    assert len(report.gates) == 1
    assert report.gates[0].passed is False
    assert "FAIL" in report.summary
