"""Require successful individual CI matrix jobs, including across failed-only reruns."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from typing import Any

REQUIRED = frozenset(
    f"verify ({os_name}, python {python})"
    for os_name in ("ubuntu-latest", "macos-latest")
    for python in ("3.12", "3.13")
)


class GuardError(Exception):
    """Missing, unsuccessful or untrustworthy CI evidence."""


def api(endpoint: str) -> object:
    """Use the step-scoped token without exposing API output/errors in logs."""
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise GuardError("Explicit Actions read token is required")
    try:
        with tempfile.TemporaryDirectory(prefix="ci-matrix-gh-") as config_dir:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "--method",
                    "GET",
                    endpoint,
                    "-H",
                    "Accept: application/vnd.github+json",
                    "-H",
                    "X-GitHub-Api-Version: 2022-11-28",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "GH_TOKEN": token,
                    "GH_PROMPT_DISABLED": "1",
                    "GH_CONFIG_DIR": config_dir,
                },
            )
        if result.returncode != 0:
            raise GuardError("Actions jobs API request failed")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise GuardError("Actions jobs API request failed or returned invalid JSON") from exc


def positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def attempt_jobs(
    repository: str,
    run_id: int,
    attempt: int,
    sha: str,
    fetch: Callable[[str], object],
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[int] = set()
    total = None
    page = 1
    run_url = f"https://api.github.com/repos/{repository}/actions/runs/{run_id}"
    while True:
        payload = fetch(
            f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs"
            f"?per_page=100&page={page}"
        )
        if not isinstance(payload, dict):
            raise GuardError("Invalid Actions jobs response")
        count, batch = payload.get("total_count"), payload.get("jobs")
        if (
            type(count) is not int
            or count < 0
            or not isinstance(batch, list)
            or len(batch) > 100
            or (total is not None and count != total)
        ):
            raise GuardError("Invalid or changing Actions jobs pagination")
        total = count
        for job in batch:
            if (
                not isinstance(job, dict)
                or not positive_int(job.get("id"))
                or job["id"] in seen
                or type(job.get("run_id")) is not int
                or job["run_id"] != run_id
                or job.get("run_url") != run_url
                or job.get("head_sha") != sha
                or not isinstance(job.get("name"), str)
                or not job["name"]
            ):
                raise GuardError("Invalid job identity or mismatched repository/run/commit")
            seen.add(job["id"])
            jobs.append(job)
        if len(jobs) > total:
            raise GuardError("Actions jobs count exceeds total_count")
        if len(jobs) == total:
            return jobs
        if len(batch) != 100:
            raise GuardError("Incomplete Actions jobs pagination")
        page += 1


def check_matrix(
    repository: str,
    run_id: int,
    attempt: int,
    sha: str,
    fetch: Callable[[str], object] = api,
) -> None:
    if (
        not isinstance(repository, str)
        or re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository) is None
        or repository.split("/")[-1] in {".", ".."}
        or not positive_int(run_id)
        or not positive_int(attempt)
        or not isinstance(sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", sha) is None
    ):
        raise GuardError("Invalid GitHub repository/run/attempt/commit context")
    latest: dict[str, dict[str, Any]] = {}
    # Explicit attempt endpoints avoid relying on the matrix summary or API default filter.
    for number in range(1, attempt + 1):
        current: dict[str, dict[str, Any]] = {}
        for job in attempt_jobs(repository, run_id, number, sha, fetch):
            name = job["name"]
            if name not in REQUIRED:
                continue
            if name in current:
                raise GuardError("Ambiguous duplicate required job in one attempt")
            current[name] = job
        latest.update(current)
    if set(latest) != REQUIRED:
        raise GuardError("Required CI matrix job missing")
    for name in sorted(REQUIRED):
        job = latest[name]
        if (
            job.get("status") != "completed"
            or job.get("conclusion") != "success"
            or not isinstance(job.get("runner_name"), str)
            or not job["runner_name"].strip()
            or not isinstance(job.get("steps"), list)
            or not job["steps"]
            or any(
                not isinstance(step, dict)
                or step.get("status") != "completed"
                or step.get("conclusion") != "success"
                for step in job["steps"]
            )
        ):
            raise GuardError(f"Required job did not execute successfully: {name}")


def main() -> int:
    try:
        run = os.environ.get("GITHUB_RUN_ID", "")
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
        if (
            re.fullmatch(r"[1-9][0-9]*", run) is None
            or re.fullmatch(r"[1-9][0-9]*", attempt) is None
        ):
            raise GuardError("Invalid GitHub run/attempt environment")
        check_matrix(
            os.environ.get("GITHUB_REPOSITORY", ""),
            int(run),
            int(attempt),
            os.environ.get("CI_HEAD_SHA", ""),
        )
    except GuardError as exc:
        print(f"CI matrix verification failed: {exc}", file=sys.stderr)
        return 1
    print("All four required CI verify jobs executed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
