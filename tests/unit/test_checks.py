from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from subsched.github.checks import CICheckState, fetch_pr_checks

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github" / "checks"


def _mock_gh(
    monkeypatch: pytest.MonkeyPatch, *, stdout: str, returncode: int, stderr: str = ""
) -> None:
    def _run(*a: object, **k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["gh"], returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", _run)


def test_fetch_pr_checks_classifications(monkeypatch: pytest.MonkeyPatch) -> None:
    # Synthetic JSON-mode fixtures: v2.100.0 exports JSON before CI exit handling (#552).
    pass_data = (_FIXTURES / "pass-exit0.json").read_text(encoding="utf-8")
    _mock_gh(monkeypatch, stdout=pass_data, returncode=0)
    status_pass = fetch_pr_checks(68)
    assert status_pass.overall_state is CICheckState.PASS
    assert len(status_pass.checks) == 2

    pending_data = (_FIXTURES / "pending-exit0.json").read_text(encoding="utf-8")
    _mock_gh(monkeypatch, stdout=pending_data, returncode=0)
    status_pending = fetch_pr_checks(68)
    assert status_pending.overall_state is CICheckState.PENDING

    fail_data = (_FIXTURES / "fail-exit0.json").read_text(encoding="utf-8")
    _mock_gh(monkeypatch, stdout=fail_data, returncode=0)
    status_fail = fetch_pr_checks(68)
    assert status_fail.overall_state is CICheckState.FAIL


def test_malformed_element_must_not_hide_behind_partial_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#376: [{pass}, 42] must be UNKNOWN overall -- a non-dict element invalidates the
    response instead of being silently dropped while the valid prefix makes it PASS."""
    payload = json.dumps(
        [
            {"name": "good", "bucket": "pass", "state": "SUCCESS", "description": "", "link": ""},
            42,
        ]
    )
    _mock_gh(monkeypatch, stdout=payload, returncode=0)

    status = fetch_pr_checks(68)

    assert status.overall_state is not CICheckState.PASS
    assert status.overall_state is CICheckState.UNKNOWN


def test_exit_code_payload_contradiction_must_not_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """#376: gh exiting 1 (some checks failed) while the JSON claims all-pass is a
    contradiction -- UNKNOWN, never PASS."""
    payload = json.dumps(
        [{"name": "good", "bucket": "pass", "state": "SUCCESS", "description": "", "link": ""}]
    )
    _mock_gh(monkeypatch, stdout=payload, returncode=1)

    status = fetch_pr_checks(68)

    assert status.overall_state is CICheckState.UNKNOWN


def test_unexpected_exit_code_must_not_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """#376: an exit code outside the documented set (0/1/7/8) is a command anomaly
    even when the payload looks like a clean pass."""
    payload = json.dumps(
        [{"name": "good", "bucket": "pass", "state": "SUCCESS", "description": "", "link": ""}]
    )
    _mock_gh(monkeypatch, stdout=payload, returncode=2)

    status = fetch_pr_checks(68)

    assert status.overall_state is CICheckState.UNKNOWN
    assert status.detail


def test_no_checks_exit_code_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """#376: gh exit 7 means no checks were reported -- that can never confirm PASS."""
    _mock_gh(monkeypatch, stdout="[]", returncode=7)

    status = fetch_pr_checks(68)

    assert status.overall_state is CICheckState.UNKNOWN


def test_empty_array_with_pass_exit_code_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful JSON export with no checks cannot establish CI success."""
    _mock_gh(monkeypatch, stdout="[]", returncode=0)

    status = fetch_pr_checks(68)

    assert status.overall_state is CICheckState.UNKNOWN


def test_missing_required_name_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """#376: an element whose required `name` is missing/empty is malformed and must
    block a PASS classification."""
    payload = json.dumps(
        [
            {"name": "good", "bucket": "pass", "state": "SUCCESS", "description": "", "link": ""},
            {"bucket": "pass", "state": "SUCCESS", "description": "", "link": ""},
        ]
    )
    _mock_gh(monkeypatch, stdout=payload, returncode=0)

    status = fetch_pr_checks(68)

    assert status.overall_state is CICheckState.UNKNOWN


def test_state_bucket_contradiction_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """#376: bucket=pass with state=FAILURE contradicts itself -- UNKNOWN, not PASS."""
    payload = json.dumps(
        [
            {
                "name": "lies",
                "bucket": "pass",
                "state": "FAILURE",
                "description": "",
                "link": "",
            }
        ]
    )
    _mock_gh(monkeypatch, stdout=payload, returncode=0)

    status = fetch_pr_checks(68)

    assert status.overall_state is CICheckState.UNKNOWN


# --- review follow-ups for #376 ----------------------------------------------------


def _element(name: str, bucket: str, state: str) -> dict[str, str]:
    return {"name": name, "bucket": bucket, "state": state, "description": "", "link": ""}


@pytest.mark.parametrize(
    "skipped",
    [_element("lint", "skipping", "SKIPPED"), _element("lint", "skipping", "NEUTRAL")],
)
def test_skipped_or_neutral_checks_do_not_block_a_green_pr(
    monkeypatch: pytest.MonkeyPatch, skipped: dict[str, str]
) -> None:
    """gh reports SKIPPED/NEUTRAL checks with bucket 'skipping' and exits 0 when
    everything else passed; such a PR must not stall as UNKNOWN."""
    payload = json.dumps([_element("build", "pass", "SUCCESS"), skipped])
    _mock_gh(monkeypatch, stdout=payload, returncode=0)

    assert fetch_pr_checks(68).overall_state is CICheckState.PASS


def test_skipped_state_alone_without_bucket_is_pass_like(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps([{"name": "lint", "state": "SKIPPED"}])
    _mock_gh(monkeypatch, stdout=payload, returncode=0)

    assert fetch_pr_checks(68).overall_state is CICheckState.PASS


@pytest.mark.parametrize("returncode", [0, 1, 8])
def test_failed_check_is_fail_whatever_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    """A payload that proves a failure must escalate (FAIL), not silently stall as
    UNKNOWN just because the exit code disagrees (e.g. a cancelled run with exit 0)."""
    payload = json.dumps(
        [_element("build", "pass", "SUCCESS"), _element("t", "cancel", "CANCELLED")]
    )
    _mock_gh(monkeypatch, stdout=payload, returncode=returncode)

    assert fetch_pr_checks(68).overall_state is CICheckState.FAIL


def test_failed_check_alongside_malformed_element_is_still_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps([_element("build", "fail", "FAILURE"), 42])
    _mock_gh(monkeypatch, stdout=payload, returncode=1)

    assert fetch_pr_checks(68).overall_state is CICheckState.FAIL


@pytest.mark.parametrize(
    ("returncode", "elements"),
    [
        (8, [_element("a", "pass", "SUCCESS")]),  # exit says pending, payload all pass
        (1, [_element("a", "pending", "QUEUED")]),  # exit says failed, payload pending
    ],
)
def test_non_failure_contradictions_stay_unknown(
    monkeypatch: pytest.MonkeyPatch, returncode: int, elements: list[dict[str, str]]
) -> None:
    _mock_gh(monkeypatch, stdout=json.dumps(elements), returncode=returncode)

    assert fetch_pr_checks(68).overall_state is CICheckState.UNKNOWN


def test_detail_redacts_secrets_from_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "ghp_" + "a" * 20
    _mock_gh(monkeypatch, stdout="not json", returncode=1, stderr=f"auth failed for {secret}")

    status = fetch_pr_checks(68)

    assert secret not in status.detail
    assert "[REDACTED]" in status.detail


@pytest.mark.parametrize("state", ["IN_PROGRESS", "QUEUED"])
@pytest.mark.parametrize("include_pass", [False, True])
def test_json_export_success_with_pending_checks(
    monkeypatch: pytest.MonkeyPatch, state: str, include_pass: bool
) -> None:
    elements = [_element("running", "pending", state)]
    if include_pass:
        elements.insert(0, _element("done", "pass", "SUCCESS"))
    _mock_gh(monkeypatch, stdout=json.dumps(elements), returncode=0)

    status = fetch_pr_checks(68)

    assert status.overall_state is CICheckState.PENDING
    assert status.detail == ""
    assert len(status.checks) == len(elements)


@pytest.mark.parametrize("returncode", [0, 8])
@pytest.mark.parametrize(
    "invalid",
    [
        42,
        {},
        {"name": "unknown", "state": "FUTURE_STATE", "bucket": "pass"},
        {"name": "unknown", "state": "SUCCESS", "bucket": "future_bucket"},
        {"name": "unknown", "state": None, "bucket": "pass"},
        {"name": "unknown", "state": "SUCCESS", "bucket": 42},
        {"name": "unknown"},
        {"name": "unknown", "state": "FUTURE_STATE"},
        {"name": "contradiction", "state": "FAILURE", "bucket": "pass"},
    ],
)
def test_pending_does_not_hide_invalid_check(
    monkeypatch: pytest.MonkeyPatch, returncode: int, invalid: object
) -> None:
    payload = json.dumps([_element("running", "pending", "QUEUED"), invalid])
    _mock_gh(monkeypatch, stdout=payload, returncode=returncode)

    assert fetch_pr_checks(68).overall_state is CICheckState.UNKNOWN


@pytest.mark.parametrize("returncode", [0, 1, 2, 4, 7, 8, 127])
@pytest.mark.parametrize("payload", ["not json", "{}", "null", "[]"])
def test_invalid_or_empty_response_never_passes(
    monkeypatch: pytest.MonkeyPatch, returncode: int, payload: str
) -> None:
    _mock_gh(monkeypatch, stdout=payload, returncode=returncode)

    assert fetch_pr_checks(68).overall_state is CICheckState.UNKNOWN


@pytest.mark.parametrize("returncode", [2, 4, 7, 127])
@pytest.mark.parametrize("bucket,state", [("pass", "SUCCESS"), ("fail", "FAILURE")])
def test_command_error_cannot_be_overridden_by_payload(
    monkeypatch: pytest.MonkeyPatch, returncode: int, bucket: str, state: str
) -> None:
    _mock_gh(
        monkeypatch,
        stdout=json.dumps([_element("check", bucket, state)]),
        returncode=returncode,
    )

    assert fetch_pr_checks(68).overall_state is CICheckState.UNKNOWN


@pytest.mark.parametrize("error", [OSError("gh unavailable"), subprocess.TimeoutExpired("gh", 30)])
def test_subprocess_error_is_unknown(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "run", fail)

    assert fetch_pr_checks(68).overall_state is CICheckState.UNKNOWN


def test_legacy_pending_exit_with_pending_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_gh(
        monkeypatch,
        stdout=json.dumps([_element("running", "pending", "QUEUED")]),
        returncode=8,
    )

    assert fetch_pr_checks(68).overall_state is CICheckState.PENDING


@pytest.mark.parametrize("returncode", [0, 1, 8])
@pytest.mark.parametrize("include_bucket", [False, True])
@pytest.mark.parametrize(
    "state,bucket,expected",
    [
        ("ACTION_REQUIRED", "fail", CICheckState.FAIL),
        ("STARTUP_FAILURE", "fail", CICheckState.FAIL),
        ("WAITING", "pending", CICheckState.PENDING),
        ("REQUESTED", "pending", CICheckState.PENDING),
    ],
)
def test_supported_check_states(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    include_bucket: bool,
    state: str,
    bucket: str,
    expected: CICheckState,
) -> None:
    element = {"name": "verify", "state": state}
    if include_bucket:
        element["bucket"] = bucket
    _mock_gh(monkeypatch, stdout=json.dumps([element]), returncode=returncode)

    status = fetch_pr_checks(68)

    assert status.checks[0].state is expected
    if returncode == 1 and expected is CICheckState.PENDING:
        expected = CICheckState.UNKNOWN
    assert status.overall_state is expected


@pytest.mark.parametrize("returncode", [0, 1, 8])
@pytest.mark.parametrize("state", ["ACTION_REQUIRED", "STARTUP_FAILURE", "WAITING", "REQUESTED"])
def test_supported_check_states_with_contradictory_bucket_stay_unknown(
    monkeypatch: pytest.MonkeyPatch, returncode: int, state: str
) -> None:
    payload = json.dumps([_element("verify", "pass", state)])
    _mock_gh(monkeypatch, stdout=payload, returncode=returncode)

    assert fetch_pr_checks(68).overall_state is CICheckState.UNKNOWN
