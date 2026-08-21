from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from enum import StrEnum


class CICheckState(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class CICheckResult:
    name: str
    state: CICheckState
    description: str
    link: str


@dataclass(frozen=True, slots=True)
class PRChecksStatus:
    pr_number: int
    overall_state: CICheckState
    checks: tuple[CICheckResult, ...]


def fetch_pr_checks(
    pr_number: int,
    repo: str | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> PRChecksStatus:
    """Fetch structured PR checks using gh pr checks and classify into PASS, FAIL, PENDING."""
    argv = [
        "gh",
        "pr",
        "checks",
        str(pr_number),
        "--json",
        "name,state,bucket,description,link",
    ]
    if repo:
        argv.extend(["--repo", repo])

    try:
        res = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
        if res.returncode != 0:
            return PRChecksStatus(
                pr_number=pr_number, overall_state=CICheckState.UNKNOWN, checks=()
            )
        data = json.loads(res.stdout)
        if not isinstance(data, list):
            return PRChecksStatus(
                pr_number=pr_number, overall_state=CICheckState.UNKNOWN, checks=()
            )

        checks: list[CICheckResult] = []
        has_fail = False
        has_pending = False

        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            bucket = str(item.get("bucket", "")).casefold()
            state_raw = str(item.get("state", "")).casefold()
            desc = str(item.get("description", ""))
            link = str(item.get("link", ""))

            if bucket == "pass" or state_raw in {"success", "pass"}:
                st = CICheckState.PASS
            elif bucket == "fail" or state_raw in {"failure", "fail", "error", "timed_out"}:
                st = CICheckState.FAIL
                has_fail = True
            elif bucket == "pending" or state_raw in {"pending", "queued", "in_progress"}:
                st = CICheckState.PENDING
                has_pending = True
            else:
                st = CICheckState.UNKNOWN

            checks.append(CICheckResult(name=name, state=st, description=desc, link=link))

        if not checks:
            overall = CICheckState.PASS
        elif has_fail:
            overall = CICheckState.FAIL
        elif has_pending:
            overall = CICheckState.PENDING
        else:
            overall = CICheckState.PASS

        return PRChecksStatus(pr_number=pr_number, overall_state=overall, checks=tuple(checks))
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return PRChecksStatus(pr_number=pr_number, overall_state=CICheckState.UNKNOWN, checks=())
