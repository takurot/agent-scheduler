from __future__ import annotations

import json
import subprocess

import pytest

from subsched.github.checks import CICheckState, fetch_pr_checks


def test_fetch_pr_checks_classifications(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. All pass
    pass_data = json.dumps([
        {"name": "pytest", "bucket": "pass", "state": "SUCCESS", "description": "", "link": ""},
        {"name": "lint", "bucket": "pass", "state": "SUCCESS", "description": "", "link": ""},
    ])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 0, stdout=pass_data, stderr=""),
    )
    status_pass = fetch_pr_checks(68)
    assert status_pass.overall_state is CICheckState.PASS
    assert len(status_pass.checks) == 2

    # 2. Has pending (even with exitcode 8)
    pending_data = json.dumps([
        {"name": "pytest", "bucket": "pass", "state": "SUCCESS", "description": "", "link": ""},
        {
            "name": "e2e",
            "bucket": "pending",
            "state": "IN_PROGRESS",
            "description": "",
            "link": "",
        },
    ])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 8, stdout=pending_data, stderr=""),
    )
    status_pending = fetch_pr_checks(68)
    assert status_pending.overall_state is CICheckState.PENDING

    # 3. Has failure (even with exitcode 1)
    fail_data = json.dumps([
        {"name": "pytest", "bucket": "fail", "state": "FAILURE", "description": "", "link": ""},
        {
            "name": "e2e",
            "bucket": "pending",
            "state": "IN_PROGRESS",
            "description": "",
            "link": "",
        },
    ])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 1, stdout=fail_data, stderr=""),
    )
    status_fail = fetch_pr_checks(68)
    assert status_fail.overall_state is CICheckState.FAIL
