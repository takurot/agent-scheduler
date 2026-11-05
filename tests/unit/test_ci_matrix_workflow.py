from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("success", [False, True])
def test_workflow_checks_individual_jobs(tmp_path: Path, success: bool) -> None:
    fixture = json.loads(
        (ROOT / "tests/fixtures/github/ci-matrix-runner-cancelled.json").read_text()
    )
    if success:
        for job in fixture["jobs"]:
            job.update(status="completed", conclusion="success", runner_name="hosted-runner")
            job["steps"] = [{"status": "completed", "conclusion": "success"}]
    fake_gh = tmp_path / "gh"
    payload = json.dumps({"total_count": 4, "jobs": fixture["jobs"]})
    fake_gh.write_text("#!/usr/bin/env python3\nprint(" + repr(payload) + ")\n")
    fake_gh.chmod(0o700)
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    command = workflow["jobs"]["all-checks"]["steps"][-1]["run"]
    # Reproduce the observed expression expansion, without invoking GitHub Actions.
    command = command.replace(
        "${{ contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled') }}",
        "false",
    )
    result = subprocess.run(
        ["bash", "-e", "-c", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "takurot/agent-scheduler",
            "GITHUB_RUN_ID": "41853570329",
            "GITHUB_RUN_ATTEMPT": "1",
            "CI_HEAD_SHA": fixture["jobs"][0]["head_sha"],
            "GH_TOKEN": "fixture-token",
        },
    )
    if success:
        assert result.returncode == 0, result.stderr
        assert "All four required" in result.stdout
    else:
        assert result.returncode != 0, result.stdout
        assert "Required job did not execute successfully" in result.stderr
