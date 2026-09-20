from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from subsched.agents.process import redact_sensitive_command_audit


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
    if bucket in {"pass", "skipping"}:
        return CICheckState.PASS
    if bucket in {"fail", "cancel"}:
        return CICheckState.FAIL
    if bucket == "pending":
        return CICheckState.PENDING
    return None


def _classify_state(state_raw: str) -> CICheckState | None:
    if state_raw in {"success", "pass", "skipped", "neutral"}:
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


def _derive_overall(checks: Sequence[CICheckResult], malformed: int) -> CICheckState:
    """Overall state implied by the payload alone. A proven failure always wins, even
    next to malformed elements; otherwise malformed/empty payloads can never be PASS."""
    states = {check.state for check in checks}
    if CICheckState.FAIL in states:
        return CICheckState.FAIL
    if not checks or malformed:
        return CICheckState.UNKNOWN
    if CICheckState.PENDING in states:
        return CICheckState.PENDING
    if CICheckState.UNKNOWN in states:
        return CICheckState.UNKNOWN
    return CICheckState.PASS


def _assess_payload(
    pr_number: int, returncode: int, data: Sequence[object], stderr: str
) -> PRChecksStatus:
    """Validate exit code and payload as a pair and produce the final status."""
    checks: list[CICheckResult] = []
    malformed = 0
    for item in data:
        check = _classify_element(item)
        if check is None:
            malformed += 1
        else:
            checks.append(check)

    derived = _derive_overall(checks, malformed)
    expected = _GH_EXIT_OVERALL[returncode]
    detail = _redact(stderr.strip())
    overall = derived
    if derived is not expected:
        # #376: a proven failure in the payload escalates whatever the exit code says
        # (FAIL can never be a false PASS, and a cancelled run must reach a human).
        # Any other disagreement (e.g. exit 1 but every element claims PASS) is
        # contradictory and cannot confirm anything.
        if derived is not CICheckState.FAIL:
            overall = CICheckState.UNKNOWN
        detail = (
            f"gh exit code {returncode} implies {expected.value}, "
            f"payload implies {derived.value}"
            + (f"; {malformed} malformed element(s)" if malformed else "")
        )
    return PRChecksStatus(
        pr_number=pr_number, overall_state=overall, checks=tuple(checks), detail=detail
    )


def _redact(text: str) -> str:
    return "\n".join(redact_sensitive_command_audit(tuple(text.splitlines())))


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
                detail=_redact(res.stderr.strip())
                if res.stderr
                else "invalid JSON response from gh",
            )

        if res.returncode not in _GH_EXIT_OVERALL and res.returncode != _GH_EXIT_NO_CHECKS:
            return PRChecksStatus(
                pr_number=pr_number,
                overall_state=CICheckState.UNKNOWN,
                checks=(),
                detail=_redact(f"unexpected gh exit code {res.returncode}: {res.stderr.strip()}"),
            )
        if res.returncode == _GH_EXIT_NO_CHECKS:
            return PRChecksStatus(
                pr_number=pr_number,
                overall_state=CICheckState.UNKNOWN,
                checks=(),
                detail="gh reported no checks for this pull request",
            )

        return _assess_payload(pr_number, res.returncode, data, res.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return PRChecksStatus(
            pr_number=pr_number,
            overall_state=CICheckState.UNKNOWN,
            checks=(),
            detail=f"{type(exc).__name__}: {exc}",
        )
