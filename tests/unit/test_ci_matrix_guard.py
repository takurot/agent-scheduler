from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import check_ci_matrix as guard

ROOT = Path(__file__).resolve().parents[2]
REPO = "takurot/agent-scheduler"
RUN = 41853570329
SHA = "cd5bda1e9ee75c5e9e1f557ffb4f7d66eb3acd1a"


def observed() -> dict[str, Any]:
    return json.loads((ROOT / "tests/fixtures/github/ci-matrix-runner-cancelled.json").read_text())


def successful() -> list[dict[str, Any]]:
    jobs = observed()["jobs"]
    for job in jobs:
        job.update(status="completed", conclusion="success", runner_name="hosted-runner")
        job["steps"] = [{"name": "Run tests", "status": "completed", "conclusion": "success"}]
    return jobs


def check(attempts: list[list[dict[str, Any]]]) -> None:
    def fetch(endpoint: str) -> object:
        attempt = int(endpoint.split("/attempts/")[1].split("/")[0])
        jobs = attempts[attempt - 1]
        return {"total_count": len(jobs), "jobs": jobs}

    guard.check_matrix(REPO, RUN, len(attempts), SHA, fetch)


def test_observed_cancelled_macos_is_rejected() -> None:
    fixture = observed()
    assert fixture["workflow_conclusion"] == "failure"
    # The summary context may say success despite the cancelled children.
    assert fixture["needs_verify_result"] == "success"
    with pytest.raises(guard.GuardError):
        check([fixture["jobs"]])


def test_all_four_successes_pass() -> None:
    check([successful()])


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "cancelled", "skipped", "startup_failure", "neutral", "timed_out", "unknown", None],
)
def test_non_success_is_rejected(conclusion: str | None) -> None:
    jobs = successful()
    jobs[-1]["conclusion"] = conclusion
    with pytest.raises(guard.GuardError):
        check([jobs])


@pytest.mark.parametrize("status", ["queued", "pending", "in_progress", "unknown", None])
def test_incomplete_is_rejected(status: str | None) -> None:
    jobs = successful()
    jobs[-1]["status"] = status
    with pytest.raises(guard.GuardError):
        check([jobs])


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "duplicate",
        "wrong_name",
        "wrong_run",
        "wrong_repo",
        "wrong_sha",
        "no_runner",
        "no_steps",
        "malformed",
        "bad_id",
    ],
)
def test_missing_or_invalid_evidence_is_rejected(problem: str) -> None:
    jobs = successful()
    if problem == "missing":
        jobs.pop()
    elif problem == "duplicate":
        jobs.append(copy.deepcopy(jobs[-1]))
        jobs[-1]["id"] += 1
    elif problem == "wrong_name":
        jobs[-1]["name"] = "verify (macos-latest, python 3.14)"
    elif problem == "wrong_run":
        jobs[-1]["run_id"] += 1
    elif problem == "wrong_repo":
        jobs[-1]["run_url"] = f"https://api.github.com/repos/other/repo/actions/runs/{RUN}"
    elif problem == "wrong_sha":
        jobs[-1]["head_sha"] = "a" * 40
    elif problem == "no_runner":
        jobs[-1]["runner_name"] = ""
    elif problem == "no_steps":
        jobs[-1]["steps"] = []
    elif problem == "malformed":
        jobs[-1] = {}
    else:
        jobs[-1]["id"] = True
    with pytest.raises(guard.GuardError):
        check([jobs])


def test_failed_only_rerun_reuses_previous_successes() -> None:
    before = observed()["jobs"]
    rerun = successful()[2:]
    for job in rerun:
        job["id"] += 100
    check([before, rerun])


def test_newer_failure_cannot_be_hidden_by_old_success() -> None:
    rerun = successful()[2:]
    for job in rerun:
        job["id"] += 100
    rerun[-1]["conclusion"] = "failure"
    with pytest.raises(guard.GuardError):
        check([successful(), rerun])


def test_unrerun_failure_remains_failure() -> None:
    with pytest.raises(guard.GuardError):
        check([observed()["jobs"], []])


def test_pagination_fetches_every_page() -> None:
    jobs = successful()
    filler = copy.deepcopy(jobs[0])
    filler["name"] = "all-checks"
    first = [{**filler, "id": 1000 + i} for i in range(100)]
    calls = []

    def fetch(endpoint: str) -> object:
        calls.append(endpoint)
        return {"total_count": 104, "jobs": first if endpoint.endswith("&page=1") else jobs}

    guard.check_matrix(REPO, RUN, 1, SHA, fetch)
    assert calls == [
        f"repos/{REPO}/actions/runs/{RUN}/attempts/1/jobs?per_page=100&page={page}"
        for page in (1, 2)
    ]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"jobs": []},
        {"total_count": True, "jobs": []},
        {"total_count": 4, "jobs": []},
        {"total_count": 0, "jobs": successful()},
        {"total_count": 1, "jobs": [None]},
    ],
)
def test_invalid_api_response_fails_closed(payload: object) -> None:
    with pytest.raises(guard.GuardError):
        guard.check_matrix(REPO, RUN, 1, SHA, lambda _: payload)


@pytest.mark.parametrize(
    "repo,run,attempt,sha",
    [
        ("../other/repo", RUN, 1, SHA),
        (REPO, 0, 1, SHA),
        (REPO, RUN, 0, SHA),
        (REPO, RUN, 1, "bad"),
        (REPO, True, 1, SHA),
    ],
)
def test_invalid_context_never_calls_api(repo: str, run: int, attempt: int, sha: str) -> None:
    def fetch(_: str) -> object:
        pytest.fail("invalid context must not reach the API")

    with pytest.raises(guard.GuardError):
        guard.check_matrix(repo, run, attempt, sha, fetch)


@pytest.mark.parametrize("problem", ["exit", "timeout", "json"])
def test_api_command_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch, problem: str) -> None:
    monkeypatch.setenv("GH_TOKEN", "fixture-token")

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[:5] == ["gh", "api", "--hostname", "github.com", "--method"]
        assert kwargs["timeout"] == 30
        if problem == "timeout":
            raise subprocess.TimeoutExpired(argv, 30, stderr="sensitive")
        return subprocess.CompletedProcess(
            argv, 1 if problem == "exit" else 0, stdout="invalid json", stderr="sensitive"
        )

    monkeypatch.setattr(guard.subprocess, "run", run)
    with pytest.raises(guard.GuardError) as error:
        guard.api("repos/example/project/actions/runs/1/attempts/1/jobs")
    assert "sensitive" not in str(error.value)


def test_workflow_wires_guard_and_keeps_full_matrix() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    verify = workflow["jobs"]["verify"]
    assert verify["name"] == "verify (${{ matrix.os }}, python ${{ matrix.python-version }})"
    matrix = verify["strategy"]["matrix"]
    assert set(guard.REQUIRED) == {
        f"verify ({os}, python {python})"
        for os in matrix["os"]
        for python in matrix["python-version"]
    }
    commands = [step["run"] for step in verify["steps"] if "run" in step]
    assert "uv run --frozen pip-audit" in commands
    assert "uv run --frozen python scripts/check_branch_coverage.py coverage.json" in commands
    assert any("--cov-fail-under=80" in command for command in commands)
    aggregate = workflow["jobs"]["all-checks"]
    assert aggregate["if"] == "always()"
    assert aggregate["needs"] == ["verify"]
    assert aggregate["permissions"] == {"contents": "read", "actions": "read"}
    assert aggregate["steps"][0]["with"]["persist-credentials"] is False
    step = aggregate["steps"][-1]
    assert step["run"] == "python3 scripts/check_ci_matrix.py"
    assert step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "CI_HEAD_SHA": "${{ github.event.pull_request.head.sha || github.sha }}",
    }


@pytest.mark.parametrize(
    "steps", [[None], [{}], [{"status": "completed", "conclusion": "skipped"}]]
)
def test_invalid_or_skipped_steps_are_rejected(steps: list[object]) -> None:
    jobs = successful()
    jobs[-1]["steps"] = steps
    with pytest.raises(guard.GuardError):
        check([jobs])


def test_api_requires_explicit_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(guard.GuardError, match="token"):
        guard.api("repos/example/project/actions/runs/1/attempts/1/jobs")


def test_api_uses_isolated_configuration_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "fixture-token")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-inherit")
    called = False

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        env = kwargs["env"]
        assert set(env) == {"PATH", "GH_TOKEN", "GH_PROMPT_DISABLED", "GH_CONFIG_DIR"}
        assert Path(env["GH_CONFIG_DIR"]).is_dir()
        assert not list(Path(env["GH_CONFIG_DIR"]).iterdir())
        return subprocess.CompletedProcess(argv, 0, stdout='{"jobs": []}', stderr="")

    monkeypatch.setattr(guard.subprocess, "run", run)
    assert guard.api("repos/example/project/actions/runs/1/attempts/1/jobs") == {"jobs": []}
    assert called
