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
    # Canonical gh outputs paired with their documented exit codes (fixture-ized per #376).
    pass_data = (_FIXTURES / "pass-exit0.json").read_text(encoding="utf-8")
    _mock_gh(monkeypatch, stdout=pass_data, returncode=0)
    status_pass = fetch_pr_checks(68)
    assert status_pass.overall_state is CICheckState.PASS
    assert len(status_pass.checks) == 2

    pending_data = (_FIXTURES / "pending-exit8.json").read_text(encoding="utf-8")
    _mock_gh(monkeypatch, stdout=pending_data, returncode=8)
    status_pending = fetch_pr_checks(68)
    assert status_pending.overall_state is CICheckState.PENDING

    fail_data = (_FIXTURES / "fail-exit1.json").read_text(encoding="utf-8")
    _mock_gh(monkeypatch, stdout=fail_data, returncode=1)
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
    """#376: exit 0 with an empty payload contradicts 'all checks passed'."""
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
