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
    detail: str = ""


# #376: gh pr checks documents these exit codes: 0 = all checks passed, 1 = some
# checks failed, 7 = no checks found, 8 = some checks pending. Anything else is a
# command anomaly and can never confirm PASS.
_GH_EXIT_OVERALL: dict[int, CICheckState] = {
    0: CICheckState.PASS,
    1: CICheckState.FAIL,
    8: CICheckState.PENDING,
}
_GH_EXIT_NO_CHECKS = 7


def _classify_bucket(bucket: str) -> CICheckState | None:
    if bucket == "pass":
        return CICheckState.PASS
    if bucket in {"fail", "cancel"}:
        return CICheckState.FAIL
    if bucket == "pending":
        return CICheckState.PENDING
    return None


def _classify_state(state_raw: str) -> CICheckState | None:
    if state_raw in {"success", "pass"}:
        return CICheckState.PASS
    if state_raw in {"failure", "fail", "error", "timed_out", "cancelled"}:
        return CICheckState.FAIL
    if state_raw in {"pending", "queued", "in_progress"}:
        return CICheckState.PENDING
    return None


def _classify_element(item: object) -> CICheckResult | None:
    """Classify one payload element, or return None when it is malformed.

    #376: an element must be a dict with a non-empty name, and its bucket/state
    values (when both present) must agree -- otherwise the element is untrustworthy
    and the response can never be PASS."""
    if not isinstance(item, dict):
        return None
    name_raw = item.get("name")
    if not isinstance(name_raw, str) or not name_raw.strip():
        return None
    bucket = str(item.get("bucket", "")).casefold()
    state_raw = str(item.get("state", "")).casefold()
    desc = str(item.get("description", ""))
    link = str(item.get("link", ""))

    bucket_state = _classify_bucket(bucket)
    state_state = _classify_state(state_raw)
    if bucket_state is not None and state_state is not None:
        st = bucket_state if bucket_state is state_state else CICheckState.UNKNOWN
    elif bucket_state is not None:
        st = bucket_state
    elif state_state is not None:
        st = state_state
    else:
        st = CICheckState.UNKNOWN
    return CICheckResult(name=name_raw, state=st, description=desc, link=link)


def fetch_pr_checks(
    pr_number: int,
    repo: str | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> PRChecksStatus:
    """Fetch structured PR checks using gh pr checks and classify into PASS, FAIL, PENDING.

    #376: the exit code and the JSON payload are validated as a pair. Malformed
    elements, missing required values, state/bucket contradictions, undocumented
    exit codes, and exit-code/payload contradictions all classify as UNKNOWN --
    never PASS (fail-closed)."""
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
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError:
            data = None

        if not isinstance(data, list):
            return PRChecksStatus(
                pr_number=pr_number,
                overall_state=CICheckState.UNKNOWN,
                checks=(),
                detail=res.stderr.strip() if res.stderr else "invalid JSON response from gh",
            )

        if res.returncode not in _GH_EXIT_OVERALL and res.returncode != _GH_EXIT_NO_CHECKS:
            return PRChecksStatus(
                pr_number=pr_number,
                overall_state=CICheckState.UNKNOWN,
                checks=(),
                detail=f"unexpected gh exit code {res.returncode}: {res.stderr.strip()}",
            )
        if res.returncode == _GH_EXIT_NO_CHECKS:
            return PRChecksStatus(
                pr_number=pr_number,
                overall_state=CICheckState.UNKNOWN,
                checks=(),
                detail="gh reported no checks for this pull request",
            )

        checks: list[CICheckResult] = []
        has_fail = False
        has_pending = False
        has_unknown = False
        malformed = 0

        for item in data:
            check = _classify_element(item)
            if check is None:
                malformed += 1
                has_unknown = True
                continue
            if check.state is CICheckState.FAIL:
                has_fail = True
            elif check.state is CICheckState.PENDING:
                has_pending = True
            elif check.state is CICheckState.UNKNOWN:
                has_unknown = True
            checks.append(check)

        if not checks or malformed:
            derived = CICheckState.UNKNOWN
        elif has_fail:
            derived = CICheckState.FAIL
        elif has_pending:
            derived = CICheckState.PENDING
        elif has_unknown:
            derived = CICheckState.UNKNOWN
        else:
            derived = CICheckState.PASS

        expected = _GH_EXIT_OVERALL[res.returncode]
        if derived is not expected:
            # #376: the payload disagrees with the exit code (e.g. gh exited 1 but
            # every element claims PASS). The response is contradictory and cannot
            # confirm anything.
            detail = (
                f"gh exit code {res.returncode} implies {expected.value}, "
                f"payload implies {derived.value}"
                + (f"; {malformed} malformed element(s)" if malformed else "")
            )
            return PRChecksStatus(
                pr_number=pr_number,
                overall_state=CICheckState.UNKNOWN,
                checks=tuple(checks),
                detail=detail,
            )

        return PRChecksStatus(
            pr_number=pr_number,
            overall_state=derived,
            checks=tuple(checks),
            detail=res.stderr.strip(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return PRChecksStatus(
            pr_number=pr_number,
            overall_state=CICheckState.UNKNOWN,
            checks=(),
            detail=f"{type(exc).__name__}: {exc}",
        )
